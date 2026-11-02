# External services and credentials

The backend's live integrations use actual provider APIs. Configure OAuth/API credentials as backend-only secrets. It also requires distinct random `DEVICE_TOKEN` and `SIMULATOR_TOKEN` values to accept device/simulator WebSocket connections. Keep all secrets out of source control and frontend build-time variables.

These external dependencies are required to exercise live provider-backed features:

| Service | Required external setup |
|---|---|
| OpenAI AI, hosted Whisper, and PCM TTS | OpenAI API key with access to selected models/endpoints. |
| Google Gemini | Google AI API key with access to selected models. Gmail OAuth is separate. |
| Ollama | Running Ollama service and reachable base URL/model; no cloud API key for local use. |
| Local Whisper/STT | A local Whisper adapter is not implemented. Compatible model files and sufficient host compute/storage would be required if added; Bengali support depends on the selected model. |
| TTS | OpenAI PCM TTS adapter is implemented; key/model access is required. |
| Gmail | Google Cloud OAuth client ID/secret, Gmail API enabled, OAuth consent configuration, and least-privilege granted scopes. Never use a Gmail account password. |
| GitHub | Personal access token with the repository/notification permissions required for configured operations; encrypt stored credentials with `INTEGRATION_ENCRYPTION_KEY` and respect GitHub rate limits. |
| LinkedIn | Approved LinkedIn application/product access, OAuth client credentials, redirect URI, and granted OpenID Connect scopes. The integration only exposes the supported member profile; do not scrape or infer inaccessible account data. |
| Weather | The implemented weather client uses Open-Meteo's public geocoding/forecast APIs and needs no API key; observe service usage terms. |
| Voice biometrics | Install `backend/requirements-voice.txt`, configure a generated Fernet `VOICE_ENCRYPTION_KEY`, and obtain/cache Resemblyzer model weights. Similarity is not liveness; the current endpoint explicitly reports anti-spoofing as unconfigured. |
| Wake-word activation | Install `backend/requirements-wakeword.txt` and provide an ONNX wake-word model trained for the configured phrase (`hey buddy` by default) via `WAKE_WORD_MODEL_PATH`; other wake labels are deliberately ignored. The model artifact is not included. |
| Encrypted personalization | Set `VOICE_ENCRYPTION_KEY` to a generated Fernet key. The user must explicitly enable personalization in the dashboard; then verified PCM/transcripts are encrypted and retained for the configured 1–90 days (30 by default), with deletion controls. |
| Production device backend | Deploy the FastAPI service on a public DNS name with TLS/WSS. The provided `buddyapi.no-idea.top` hostname did not resolve from this development environment; confirm DNS and service deployment before provisioning the ESP32. |
| Local Whisper STT | Install `backend/requirements-local-whisper.txt` and provision the Faster-Whisper model weights; download/runtime behavior depends on model choice and available device resources. |
| Voice liveness/anti-spoofing | Provide a compatible, trained ONNX model via `ANTI_SPOOF_MODEL_PATH` and validate its bona-fide class/threshold against real speakers and replay/spoof samples. The command gate fails closed when the model is unavailable. |
| Firmware OTA | Configure a trusted HTTPS backend URL, registered device bearer credential, matching `hardware_target`, and uploaded ESP image. The device checks SHA-256 against the authenticated manifest; publisher-signature verification is not implemented. OTA transfer and dual-slot rollback compile but have not been exercised on hardware. |
| Shared rate limiting | Configure Redis using `REDIS_URL` for cross-process rate enforcement. If Redis is unavailable, the application logs the condition and falls back to process-local limits, which do not coordinate across workers. |
| Physical device | ESP32-S3 N16R8, INMP441, MAX98357A, speaker, selected display, appropriate power, flashing, and electrical/acoustic validation. None were available for this software run. |
| Firmware time and TLS | Device network access to DNS and `pool.ntp.org` is used for clock synchronization before TLS WebSocket setup; `wss://` uses the ESP-IDF certificate bundle. Local `ws://` is unencrypted and suitable only on a trusted network. |

OAuth/API features still require live credentials, scopes, account approvals, and external verification; follow the remaining items in [`../IMPLEMENTATION_STATUS.md`](../IMPLEMENTATION_STATUS.md). LinkedIn is restricted to the implemented authorized OpenID profile endpoint; messaging and analytics are not claimed or scraped.

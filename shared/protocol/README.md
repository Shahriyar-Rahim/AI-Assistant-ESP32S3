# Protocol v1

Control messages are UTF-8 JSON matching `control.schema.json`. Each control message has a protocol version, unique message ID, device ID, UTC timestamp, type, and object payload. Audio uses a WebSocket binary message, never unframed PCM mixed with JSON.

## Binary PCM frame

All integers are unsigned, network byte order. The 25-byte header is followed by exactly `sample_count * channels * 2` bytes of signed little-endian PCM:

| Offset | Size | Field |
|---:|---:|---|
| 0 | 4 | ASCII magic `EAIA` |
| 4 | 1 | frame version (`1`) |
| 5 | 4 | monotonically increasing sequence number |
| 9 | 8 | capture timestamp, Unix epoch microseconds |
| 17 | 4 | sample rate (16,000 Hz for this system) |
| 21 | 2 | sample count per channel |
| 23 | 1 | channel count (`1`) |
| 24 | 1 | encoding (`1` = signed PCM16 little-endian) |

The backend rejects unsupported metadata, malformed lengths, and frames above `MAX_AUDIO_FRAME_BYTES`. WebSocket connections are authenticated with a device-specific bearer token. Audio is processed in bounded chunks and is not retained by default.

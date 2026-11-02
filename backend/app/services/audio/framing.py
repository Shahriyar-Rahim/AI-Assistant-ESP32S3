from __future__ import annotations

import math
import struct
from dataclasses import dataclass

MAGIC = b"EAIA"
FRAME_VERSION = 1
ENCODING_PCM16_LE = 1
HEADER = struct.Struct("!4sBIQI HBB".replace(" ", ""))


class AudioFrameError(ValueError):
    pass


@dataclass(frozen=True)
class AudioFrame:
    sequence: int
    timestamp_us: int
    sample_rate: int
    sample_count: int
    channels: int
    pcm: bytes

    @classmethod
    def decode(cls, packet: bytes, max_bytes: int) -> "AudioFrame":
        if len(packet) > max_bytes:
            raise AudioFrameError("audio frame exceeds configured size limit")
        if len(packet) < HEADER.size:
            raise AudioFrameError("audio frame is shorter than its header")
        magic, version, sequence, timestamp_us, rate, count, channels, encoding = HEADER.unpack_from(packet)
        if magic != MAGIC or version != FRAME_VERSION:
            raise AudioFrameError("unsupported audio frame magic or version")
        if rate != 16_000 or channels != 1 or encoding != ENCODING_PCM16_LE:
            raise AudioFrameError("unsupported audio format; expected 16 kHz mono PCM16 LE")
        if count == 0:
            raise AudioFrameError("audio frame must contain samples")
        pcm = packet[HEADER.size:]
        if len(pcm) != count * channels * 2:
            raise AudioFrameError("audio payload length does not match sample count")
        return cls(sequence, timestamp_us, rate, count, channels, pcm)

    def rms(self) -> int:
        sample_count = len(self.pcm) // 2
        if sample_count == 0:
            return 0
        samples = struct.iter_unpack("<h", self.pcm)
        mean_square = sum(sample[0] * sample[0] for sample in samples) / sample_count
        return math.isqrt(round(mean_square))


def encode_frame(frame: AudioFrame) -> bytes:
    if len(frame.pcm) != frame.sample_count * frame.channels * 2:
        raise AudioFrameError("audio payload length does not match sample count")
    return HEADER.pack(
        MAGIC, FRAME_VERSION, frame.sequence, frame.timestamp_us, frame.sample_rate,
        frame.sample_count, frame.channels, ENCODING_PCM16_LE,
    ) + frame.pcm

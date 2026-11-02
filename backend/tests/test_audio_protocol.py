import math
import struct

import pytest

from app.services.audio.framing import AudioFrame, AudioFrameError, encode_frame


def test_audio_frame_round_trip() -> None:
    pcm = struct.pack("<4h", 1000, -1000, 1000, -1000)
    frame = AudioFrame(7, 1_800_000_000_000_000, 16_000, 4, 1, pcm)
    decoded = AudioFrame.decode(encode_frame(frame), 1024)
    assert decoded == frame
    assert decoded.rms() == 1000


@pytest.mark.parametrize("packet", [b"", b"EAIA", b"NOPE" + bytes(32)])
def test_audio_frame_rejects_bad_headers(packet: bytes) -> None:
    with pytest.raises(AudioFrameError):
        AudioFrame.decode(packet, 1024)


def test_audio_frame_rejects_bad_sample_length_and_size() -> None:
    frame = AudioFrame(0, 1, 16_000, 2, 1, struct.pack("<h", 5))
    with pytest.raises(AudioFrameError, match="payload length"):
        AudioFrame.decode(encode_frame(frame), 1024)
    valid = encode_frame(AudioFrame(0, 1, 16_000, 1, 1, struct.pack("<h", 5)))
    with pytest.raises(AudioFrameError, match="size limit"):
        AudioFrame.decode(valid, len(valid) - 1)


def test_rms_is_zero_for_zero_pcm() -> None:
    frame = AudioFrame(0, 1, 16_000, 3, 1, struct.pack("<3h", 0, 0, 0))
    assert frame.rms() == math.isqrt(0)

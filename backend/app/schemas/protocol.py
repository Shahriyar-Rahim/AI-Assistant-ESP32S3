from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ControlType = Literal[
    "device.hello",
    "device.status",
    "audio.start",
    "audio.end",
    "audio.vad",
    "audio.wake_word",
    "audio.transcription",
    "audio.ack",
    "audio.nack",
    "biometric.result",
    "assistant.response",
    "tts.start",
    "tts.chunk",
    "tts.end",
    "telemetry.sample",
    "display.state",
    "animation.state",
    "sound.effect",
    "device.brightness",
    "device.microphone",
    "ota.status",
    "ota.available",
    "calendar.notification",
    "game.state",
    "game.command",
    "game.event",
    "device.setup",
    "schedule.sync",
    "schedule.fired",
    "game.history",
    "weather.alert",
    "device.error",
    "ping",
    "pong",
]


class ControlMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1]
    message_id: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    type: ControlType
    payload: dict[str, Any]

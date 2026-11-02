#!/usr/bin/env python3
"""SIMULATED device client; it does not validate physical hardware."""

from __future__ import annotations

import asyncio
import json
import os
import struct
import time
import uuid
import sys
from pathlib import Path

from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.audio.framing import AudioFrame, encode_frame


async def run() -> None:
    backend_url = os.getenv("BACKEND_URL", "ws://localhost:8000/api/v1/ws")
    device_id = os.getenv("DEVICE_ID", "sim-device-001")
    token = os.getenv("SIMULATOR_TOKEN", "")
    if not token:
        raise RuntimeError("Set SIMULATOR_TOKEN to the backend simulator token")
    url = f"{backend_url.rstrip('/')}/devices/{device_id}/audio"
    async with connect(url, additional_headers={"Authorization": f"Bearer {token}"}) as websocket:
        await websocket.send(json.dumps({
                "protocol_version": 1,
                "message_id": str(uuid.uuid4()),
                "device_id": device_id,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "type": "device.hello",
                "payload": {"origin": "SIMULATED", "firmware": "simulator"},
        }))
        print("SIMULATED device connected:", await websocket.recv())
        sequence = 0
        while True:
            samples = [1600 if sequence % 6 < 3 else 0] * 320
            frame = AudioFrame(
                sequence=sequence,
                timestamp_us=time.time_ns() // 1000,
                sample_rate=16_000,
                sample_count=len(samples),
                channels=1,
                pcm=struct.pack(f"<{len(samples)}h", *samples),
            )
            await websocket.send(encode_frame(frame))
            try:
                print("SIMULATED event:", await asyncio.wait_for(websocket.recv(), timeout=0.02))
            except TimeoutError:
                pass
            sequence = (sequence + 1) & 0xFFFFFFFF
            await asyncio.sleep(0.02)


if __name__ == "__main__":
    asyncio.run(run())

#!/usr/bin/env python3
"""SIMULATED device client; it does not validate physical hardware."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import time
import uuid
import sys
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.audio.framing import AudioFrame, encode_frame

PCM_BLOCK_BYTES = 320 * 2
MAX_PCM_FILE_BYTES = 4 * 1024 * 1024
OBSERVABLE_EVENTS = {
    "display.state", "game.state", "calendar.notification", "sound.effect",
    "animation.state", "ota.available", "assistant.response", "biometric.result",
    "audio.transcription", "tts.start", "tts.end", "device.error",
}


def load_pcm_file(path: str | Path) -> bytes:
    source = Path(path)
    if source.stat().st_size == 0 or source.stat().st_size > MAX_PCM_FILE_BYTES:
        raise ValueError("PCM file must contain 20 ms, 16 kHz mono PCM16 blocks and be at most 4 MiB")
    pcm = source.read_bytes()
    if len(pcm) % PCM_BLOCK_BYTES:
        raise ValueError("PCM file must contain 20 ms, 16 kHz mono PCM16 blocks and be at most 4 MiB")
    return pcm


def audio_packet(sequence: int, pcm: bytes) -> bytes:
    if len(pcm) != PCM_BLOCK_BYTES:
        raise ValueError("simulator audio blocks must contain exactly 320 PCM16 samples")
    return encode_frame(AudioFrame(
        sequence=sequence,
        timestamp_us=time.time_ns() // 1000,
        sample_rate=16_000,
        sample_count=320,
        channels=1,
        pcm=pcm,
    ))


def _hello(device_id: str) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "message_id": f"sim-boot-{uuid.uuid4()}",
        "device_id": device_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "type": "device.hello",
        "payload": {"origin": "SIMULATED", "firmware": "simulator"},
    }


async def _connect_device(url: str, device_id: str, token: str):
    websocket = await connect(url, additional_headers={"Authorization": f"Bearer {token}"})
    try:
        await websocket.send(json.dumps(_hello(device_id)))
        status = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
        if status.get("type") != "device.status":
            raise RuntimeError(f"Expected device.status, received {status.get('type')}")
        print("SIMULATED device connected:", json.dumps(status))
        return websocket
    except Exception:
        await websocket.close()
        raise


async def _receive_until(websocket, expected_type: str, timeout: float) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for {expected_type}")
        event = json.loads(await asyncio.wait_for(websocket.recv(), timeout=remaining))
        print("SIMULATED event:", json.dumps(event))
        if event.get("type") == expected_type:
            return event


async def _replay_gap(websocket) -> None:
    silent = bytes(PCM_BLOCK_BYTES)
    await websocket.send(audio_packet(0, silent))
    await _receive_until(websocket, "audio.ack", 5)
    await websocket.send(audio_packet(2, silent))
    nack = await _receive_until(websocket, "audio.nack", 5)
    expected = nack.get("payload", {}).get("expected_sequence")
    if expected != 1:
        raise RuntimeError(f"Replay scenario expected missing sequence 1, received {expected}")
    await websocket.send(audio_packet(1, silent))
    await websocket.send(audio_packet(2, silent))
    acknowledgements: set[int] = set()
    deadline = asyncio.get_running_loop().time() + 5
    while acknowledgements != {1, 2}:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for replay acknowledgements")
        event = json.loads(await asyncio.wait_for(websocket.recv(), timeout=remaining))
        print("SIMULATED event:", json.dumps(event))
        if event.get("type") == "audio.ack":
            acknowledgements.add(event.get("payload", {}).get("sequence"))
    print("SIMULATED replay scenario observed ACKs for sequences 1 and 2")


async def _send_pcm(websocket, pcm: bytes) -> None:
    for sequence, offset in enumerate(range(0, len(pcm), PCM_BLOCK_BYTES)):
        await websocket.send(audio_packet(sequence, pcm[offset:offset + PCM_BLOCK_BYTES]))
        await asyncio.sleep(0.02)


async def _observe_pcm(websocket, sender: asyncio.Task, timeout: float) -> None:
    terminal = {"tts.end", "device.error"}
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("No terminal voice-pipeline event was received")
            try:
                raw_message = await asyncio.wait_for(websocket.recv(), timeout=min(remaining, 5))
            except TimeoutError:
                if sender.done():
                    sender.result()
                    continue
                continue
            if isinstance(raw_message, bytes):
                print(f"SIMULATED binary response: {len(raw_message)} bytes")
                continue
            event = json.loads(raw_message)
            print("SIMULATED event:", json.dumps(event))
            if event.get("type") == "biometric.result" and not event.get("payload", {}).get("accepted"):
                return
            if event.get("type") in terminal:
                return
    finally:
        if not sender.done():
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)


async def run(scenario: str, pcm_file: str | None, event_timeout: float) -> None:
    backend_url = os.getenv("BACKEND_URL", "ws://localhost:8000/api/v1/ws")
    device_id = os.getenv("DEVICE_ID", "sim-device-001")
    token = os.getenv("SIMULATOR_TOKEN", "")
    if not token:
        raise RuntimeError("Set SIMULATOR_TOKEN to the backend simulator token")
    url = f"{backend_url.rstrip('/')}/devices/{device_id}/audio"

    websocket = await _connect_device(url, device_id, token)
    try:
        if scenario == "replay-gap":
            await _replay_gap(websocket)
        elif scenario == "observe":
            expected = set(filter(None, os.getenv("SIMULATOR_EXPECT", "").split(",")))
            deadline = asyncio.get_running_loop().time() + event_timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    if expected:
                        raise TimeoutError(f"No matching simulated control event received: {sorted(expected)}")
                    raise TimeoutError("No observable simulated control event received")
                raw_message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                if isinstance(raw_message, bytes):
                    print(f"SIMULATED binary event: {len(raw_message)} bytes")
                    continue
                event = json.loads(raw_message)
                print("SIMULATED event:", json.dumps(event))
                event_type = event.get("type")
                if expected:
                    expected.discard(event_type)
                    if not expected:
                        break
                elif event_type in OBSERVABLE_EVENTS:
                    break
        elif scenario == "pcm":
            if pcm_file is None:
                raise ValueError("The pcm scenario requires --pcm-file with an explicit recorded PCM fixture")
            pcm = load_pcm_file(pcm_file)
            print(f"SIMULATED PCM fixture: {len(pcm) // PCM_BLOCK_BYTES} frames; not physical microphone evidence")
            sender = asyncio.create_task(_send_pcm(websocket, pcm))
            await _observe_pcm(websocket, sender, event_timeout)
            await asyncio.gather(sender, return_exceptions=True)
        else:
            sequence = 0
            while True:
                samples = [1600 if sequence % 6 < 3 else 0] * 320
                pcm = struct.pack("<320h", *samples)
                await websocket.send(audio_packet(sequence, pcm))
                try:
                    event = await asyncio.wait_for(websocket.recv(), timeout=0.02)
                    print("SIMULATED event:", event)
                except TimeoutError:
                    pass
                sequence = (sequence + 1) & 0xFFFFFFFF
                await asyncio.sleep(0.02)
    finally:
        await websocket.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("stream", "replay-gap", "reconnect", "observe", "pcm"),
        default="stream",
    )
    parser.add_argument("--pcm-file", help="raw 16 kHz mono PCM16 fixture for --scenario pcm")
    parser.add_argument("--event-timeout", type=float, default=120.0)
    args = parser.parse_args()
    if args.event_timeout <= 0:
        parser.error("--event-timeout must be positive")

    async def execute() -> None:
        if args.scenario == "reconnect":
            backend_url = os.getenv("BACKEND_URL", "ws://localhost:8000/api/v1/ws")
            device_id = os.getenv("DEVICE_ID", "sim-device-001")
            token = os.getenv("SIMULATOR_TOKEN", "")
            if not token:
                raise RuntimeError("Set SIMULATOR_TOKEN to the backend simulator token")
            url = f"{backend_url.rstrip('/')}/devices/{device_id}/audio"
            for attempt in range(2):
                websocket = await _connect_device(url, device_id, token)
                await websocket.close()
                print(f"SIMULATED reconnect cycle {attempt + 1}/2 observed")
        else:
            await run(args.scenario, args.pcm_file, args.event_timeout)

    asyncio.run(execute())


if __name__ == "__main__":
    main()

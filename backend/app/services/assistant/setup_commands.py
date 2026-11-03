"""Voice commands that open the device's Wi-Fi setup portal or erase its saved setup."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from app.services.assistant.device_games import is_physical_device

logger = logging.getLogger(__name__)

CONFIRM_SECONDS = 25.0
SETTLE_SECONDS = 1.5

_PORTAL = re.compile(
    r"\b(?:open|start|turn on|enable|launch|show)\b.*\b(?:ap|a p|access point|setup|set up|wi-?fi|configuration|config)\s*(?:portal|mode|page|hotspot)\b|"
    r"\b(?:change|switch|update|new)\s+(?:the\s+|your\s+|my\s+)?wi-?fi\b|\bconnect\s+(?:to\s+)?(?:a\s+)?(?:new|different)\s+wi-?fi\b|"
    r"\bopen\s+(?:your\s+)?(?:ap|a p)\b"
)
_ERASE = re.compile(
    r"\b(?:erase|erace|a\s+race|arrays|wipe|reset|forget|format|clear|delete)\s+(?:out\s+)?(?:your\s*self|yourself|your\s+(?:own\s+)?"
    r"(?:setup|set\s+up|settings|configuration|config|memory|data|wi-?fi)|everything|the\s+(?:setup|settings|device)|all\s+(?:data|settings))\b|"
    r"\bfactory\s+(?:reset|settings)\b|\b(?:erase|wipe|reset)\s+(?:the\s+)?(?:setup|settings)\b|\bstart\s+(?:over|fresh)\b"
)
_YES = re.compile(r"\b(?:yes|yeah|yep|confirm|confirmed|do it|go ahead|sure|affirmative)\b")
_NO = re.compile(r"\b(?:no|nope|cancel|stop|don'?t|never mind|abort)\b")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s'-]", " ", text.casefold())).strip()


async def _send_setup(application, websocket, device_id: str, action: str) -> None:
    try:
        await asyncio.sleep(SETTLE_SECONDS)
        while (remaining := application.state.audio_echo_until.get(device_id, 0.0) - time.monotonic()) > 0:
            await asyncio.sleep(remaining)
        await asyncio.sleep(0.5)
        await websocket.send_json({
            "protocol_version": 1,
            "message_id": f"setup-{action}",
            "device_id": device_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "device.setup",
            "payload": {"action": action},
        })
    except Exception:
        logger.warning("Could not send %s to %s", action, device_id, exc_info=True)


async def route_setup_command(application, websocket, device_id: str, transcript: str) -> str | None:
    """Returns the spoken reply for a setup command, or None when the transcript is something else."""
    if websocket is None or not is_physical_device(application, device_id):
        return None
    lowered = _normalize(transcript)
    pending = application.state.pending_setup
    expiry = pending.get(device_id)
    if expiry is not None:
        pending.pop(device_id, None)
        if expiry > time.monotonic():
            if _YES.search(lowered) and not _NO.search(lowered):
                asyncio.create_task(_send_setup(application, websocket, device_id, "erase_setup"))
                return ("Erasing my setup now. I will restart and open my setup access point. "
                        "Connect to it from your phone or computer to set me up again.")
            return "Okay, I will keep my setup."
    if _ERASE.search(lowered):
        logger.info("Erase request recognised for %s", device_id)
        pending[device_id] = time.monotonic() + CONFIRM_SECONDS
        application.state.relisten[device_id] = True
        return "This erases my Wi-Fi and backend setup. Do you want me to erase myself? Say yes to confirm."
    if _PORTAL.search(lowered):
        asyncio.create_task(_send_setup(application, websocket, device_id, "open_portal"))
        return ("Opening my setup access point. I will disconnect from the backend. "
                "Join the Buddy Setup Wi-Fi and open 192 dot 168 dot 4 dot 1.")
    return None

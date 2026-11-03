"""Device location from the setup page; the board has no GPS so the user selects it."""

from __future__ import annotations

import logging
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.models import Device

logger = logging.getLogger(__name__)
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"


def sanitize_location(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    country = str(raw.get("country") or "").strip().upper()
    timezone_name = str(raw.get("timezone") or "").strip()
    city = re.sub(r"\s+", " ", str(raw.get("city") or "")).strip()[:40]
    if country and not re.fullmatch(r"[A-Z]{2}", country):
        country = ""
    if timezone_name:
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            timezone_name = ""
    if not (country or timezone_name or city):
        return None
    return {"country": country, "timezone": timezone_name, "city": city}


async def geocode_city(city: str, country: str) -> dict[str, Any] | None:
    params: dict[str, Any] = {"name": city, "count": 1, "language": "en", "format": "json"}
    if country:
        params["countryCode"] = country
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3)) as client:
            response = await client.get(GEOCODING_URL, params=params)
        response.raise_for_status()
        results = response.json().get("results") or []
    except (httpx.HTTPError, ValueError):
        logger.warning("City geocoding failed for %r", city)
        return None
    if not results:
        return None
    first = results[0]
    if first.get("latitude") is None or first.get("longitude") is None:
        return None
    return first


async def apply_hello_location(application, device_id: str, raw: Any) -> dict[str, Any] | None:
    """Stores the selected location and resolves the city to weather coordinates."""
    import asyncio

    location = sanitize_location(raw)
    if location is None:
        return None
    geocoded = await geocode_city(location["city"], location["country"]) if location["city"] else None
    if geocoded and not location["timezone"] and geocoded.get("timezone"):
        location["timezone"] = str(geocoded["timezone"])

    def save() -> None:
        with application.state.db_factory() as db:
            device = db.get(Device, device_id)
            if device is None:
                return
            config = dict(device.config or {})
            config["location"] = location
            existing = config.get("display_location") or {}
            if geocoded and (not existing or existing.get("source") == "setup"):
                config["display_location"] = {
                    "latitude": geocoded["latitude"],
                    "longitude": geocoded["longitude"],
                    "name": geocoded.get("name", location["city"]),
                    "source": "setup",
                }
            device.config = config
            db.commit()

    await asyncio.to_thread(save)
    return location

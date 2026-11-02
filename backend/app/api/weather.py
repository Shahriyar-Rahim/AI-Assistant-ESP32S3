import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.dependencies import require_user
from app.models import AuditLog

router = APIRouter()
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


@router.get("/weather/current")
async def current_weather(
    request: Request,
    latitude: float = Query(ge=-90, le=90),
    longitude: float = Query(ge=-180, le=180),
    timezone_name: str = "auto",
    _: object = Depends(require_user),
) -> dict[str, object]:
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(FORECAST_URL, params={
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": timezone_name,
                "forecast_days": 3,
            })
        response.raise_for_status()
        result = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"Weather service returned HTTP {exc.response.status_code}") from exc
    except httpx.TransportError as exc:
        raise HTTPException(502, "Weather service connection failed") from exc
    with request.app.state.db_factory() as db:
        db.add(AuditLog(event_type="weather.lookup", source="dashboard", action="forecast lookup", result="success",
                        metadata_json={"latitude": latitude, "longitude": longitude}))
        db.commit()
    return result


@router.get("/weather/geocode")
async def geocode_location(name: str = Query(min_length=1, max_length=100), _: object = Depends(require_user)) -> dict[str, object]:
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(GEOCODING_URL, params={"name": name, "count": 10, "language": "en", "format": "json"})
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"Geocoding service returned HTTP {exc.response.status_code}") from exc
    except httpx.TransportError as exc:
        raise HTTPException(502, "Geocoding service connection failed") from exc
    locations = payload.get("results", [])
    return {"locations": [{
        "name": row["name"],
        "country": row.get("country"),
        "admin1": row.get("admin1"),
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "timezone": row.get("timezone"),
    } for row in locations]}

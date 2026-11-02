from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.models import AuditLog, AutomationEndpoint, EndpointHealthCheck


def validate_endpoint_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("endpoint URL must use HTTP or HTTPS and include a hostname")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("endpoint URL cannot contain credentials, query parameters, or a fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("endpoint URL contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("endpoint URL contains an invalid port")
    return url


async def _ensure_public_host(host: str, port: int) -> None:
    try:
        records = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("endpoint hostname did not resolve") from exc
    addresses = {item[4][0] for item in records}
    if not addresses:
        raise ValueError("endpoint hostname did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("endpoint must resolve only to public IP addresses")


async def check_endpoint(application, endpoint_id: str) -> dict[str, object]:
    with application.state.db_factory() as db:
        endpoint = db.get(AutomationEndpoint, endpoint_id)
        if endpoint is None:
            raise LookupError("health-check endpoint not found")
        endpoint_url = endpoint.url
        endpoint_name = endpoint.name
    started = time.monotonic()
    status_code: int | None = None
    error: str | None = None
    result = "failure"
    try:
        parsed = urlsplit(validate_endpoint_url(endpoint_url))
        await _ensure_public_host(parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80))
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=3), follow_redirects=False) as client:
            response = await client.get(endpoint_url, headers={"User-Agent": "EdgeAssistant-HealthMonitor/1.0"})
        status_code = response.status_code
        result = "success" if status_code < 400 else "failure"
        if result == "failure":
            error = f"HTTP {status_code}"
    except ValueError as exc:
        error = str(exc)
    except httpx.TimeoutException:
        error = "request timed out"
    except httpx.TransportError:
        error = "connection failed"
    elapsed_ms = round((time.monotonic() - started) * 1000)
    checked_at = datetime.now(timezone.utc)
    with application.state.db_factory() as db:
        endpoint = db.get(AutomationEndpoint, endpoint_id)
        if endpoint is None:
            raise LookupError("health-check endpoint was removed during its check")
        endpoint.last_checked_at = checked_at
        endpoint.last_status = result
        endpoint.last_response_ms = elapsed_ms
        endpoint.failure_count = endpoint.failure_count + 1 if result == "failure" else 0
        db.add(EndpointHealthCheck(
            id=str(uuid4()),
            endpoint_id=endpoint_id,
            checked_at=checked_at,
            http_status=status_code,
            response_ms=elapsed_ms,
            result=result,
            error=error,
        ))
        db.add(AuditLog(
            event_type="automation.health_check",
            source="scheduler",
            action="HTTP endpoint ping",
            result=result,
            metadata_json={"endpoint_id": endpoint_id, "endpoint_name": endpoint_name, "http_status": status_code, "response_ms": elapsed_ms, "error": error},
        ))
        db.commit()
    return {
        "endpoint_id": endpoint_id,
        "name": endpoint_name,
        "status": result,
        "http_status": status_code,
        "response_ms": elapsed_ms,
        "error": error,
        "checked_at": checked_at.isoformat(),
    }


def due_endpoint_ids(application) -> list[str]:
    now = datetime.now(timezone.utc)
    with application.state.db_factory() as db:
        endpoints = db.execute(select(AutomationEndpoint).where(AutomationEndpoint.enabled.is_(True))).scalars().all()
        return [
            endpoint.id for endpoint in endpoints
            if endpoint.last_checked_at is None
            or (now - (endpoint.last_checked_at.replace(tzinfo=timezone.utc) if endpoint.last_checked_at.tzinfo is None else endpoint.last_checked_at)).total_seconds() >= endpoint.interval_seconds
        ]

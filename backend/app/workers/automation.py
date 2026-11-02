from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.models import AuditLog, AutomationEndpoint, AutomationRule, Notification
from app.workers.calendar import broadcast_notification

logger = logging.getLogger(__name__)
WEATHER_METRICS = {
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
}


def _due(rule: AutomationRule, now: datetime) -> bool:
    if rule.last_triggered_at is None:
        return True
    last = rule.last_triggered_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() >= rule.cooldown_seconds


async def _weather_trigger(conditions: dict) -> tuple[bool, dict[str, object]]:
    metric = conditions["metric"]
    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=3)) as client:
        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": conditions["latitude"],
                "longitude": conditions["longitude"],
                "current": metric,
                "timezone": "auto",
            },
        )
    response.raise_for_status()
    current = response.json().get("current", {})
    value = current.get(metric)
    if not isinstance(value, (int, float)):
        raise ValueError(f"weather response omitted configured metric {metric}")
    matched = value >= conditions["threshold"] if conditions["operator"] == "gte" else value <= conditions["threshold"]
    return matched, {"metric": metric, "value": value, "threshold": conditions["threshold"]}


def _endpoint_trigger(db, conditions: dict) -> tuple[bool, dict[str, object]]:
    endpoint = db.get(AutomationEndpoint, conditions["endpoint_id"])
    if endpoint is None:
        return False, {"reason": "endpoint_missing"}
    matched = endpoint.failure_count >= conditions["minimum_failures"]
    return matched, {
        "endpoint_id": endpoint.id,
        "failure_count": endpoint.failure_count,
        "minimum_failures": conditions["minimum_failures"],
        "status": endpoint.last_status,
    }


async def evaluate_automation_rules(application) -> int:
    now = datetime.now(timezone.utc)
    with application.state.db_factory() as db:
        rules = db.execute(select(AutomationRule).where(AutomationRule.enabled.is_(True))).scalars().all()
        snapshots = [{
            "id": rule.id,
            "name": rule.name,
            "trigger_type": rule.trigger_type,
            "conditions": dict(rule.conditions),
            "action": dict(rule.action),
            "cooldown_seconds": rule.cooldown_seconds,
            "last_triggered_at": rule.last_triggered_at,
        } for rule in rules if _due(rule, now)]
    triggered = 0
    for snapshot in snapshots:
        try:
            if snapshot["trigger_type"] == "weather":
                matched, evidence = await _weather_trigger(snapshot["conditions"])
            elif snapshot["trigger_type"] == "endpoint_failure":
                with application.state.db_factory() as db:
                    matched, evidence = _endpoint_trigger(db, snapshot["conditions"])
            else:
                logger.error("Automation rule %s has unsupported trigger type", snapshot["id"])
                continue
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("Automation rule %s evaluation failed: %s", snapshot["id"], exc)
            continue
        if not matched:
            continue
        occurrence = now.replace(second=0, microsecond=0)
        with application.state.db_factory() as db:
            rule = db.get(AutomationRule, snapshot["id"])
            if rule is None or not rule.enabled or not _due(rule, now):
                continue
            title = str(snapshot["action"]["title"])
            notification = Notification(
                id=str(uuid4()),
                source_type="automation_rule",
                source_id=rule.id,
                occurrence_at=occurrence,
                title=title,
                payload={"rule_id": rule.id, "rule_name": rule.name, "trigger": evidence},
            )
            db.add(notification)
            rule.last_triggered_at = now
            db.add(AuditLog(
                event_type="automation.rule_triggered",
                source="scheduler",
                action="automation notification created",
                result="success",
                metadata_json={"rule_id": rule.id, "rule_name": rule.name, "trigger": evidence},
            ))
            try:
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Could not persist automation rule trigger %s", rule.id)
                continue
        triggered += 1
        await broadcast_notification(application, {
            "id": notification.id,
            "source_type": "automation_rule",
            "title": notification.title,
            "payload": notification.payload,
        })
    return triggered


async def automation_rule_loop(application) -> None:
    while True:
        try:
            await evaluate_automation_rules(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automation rule scheduler failed")
        await asyncio.sleep(30)

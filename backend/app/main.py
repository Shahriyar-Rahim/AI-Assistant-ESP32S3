from contextlib import asynccontextmanager
import asyncio
import logging
import math
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from collections import defaultdict, deque

from app.api.auth import router as auth_router
from app.api.audit import router as audit_router
from app.api.audio import router as audio_router
from app.api.biometric import router as biometric_router
from app.api.personalization import router as personalization_router
from app.api.ai import router as ai_router
from app.api.automation import router as automation_router
from app.api.calendar import router as calendar_router
from app.api.devices import router as devices_router
from app.api.games import router as games_router
from app.api.health import router as health_router
from app.api.integrations import router as integrations_router
from app.api.news import router as news_router
from app.api.brain import router as brain_router
from app.api.weather import router as weather_router
from app.api.monitoring import router as monitoring_router
from app.api.display import router as display_router
from app.api.ota import router as ota_router
from app.api.alert_rules import router as alert_rules_router
from app.api.vision import router as vision_router
from app.services.vision.monitor import VisionMonitor
from app.core.config import Settings
from app.core.security import hash_password
from app.database.session import Base, create_database
from app.models import User
from app import models  # noqa: F401 - register ORM models before create_all
from app.workers.calendar import scheduler_loop
from app.websocket.device import router as device_ws_router, telemetry_broadcast_loop
from app.workers.automation import automation_rule_loop
from app.services.assistant.personalization import voice_retention_loop

logger = logging.getLogger(__name__)

# Uvicorn only configures its own loggers; without a handler the application's INFO diagnostics
# (wake scores, audio stream, TTS) are silently dropped.
_app_logger = logging.getLogger("app")
if not _app_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    _app_logger.addHandler(_handler)
    _app_logger.setLevel(logging.INFO)


class ApiRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, requests_per_minute: int = 120, redis_client=None):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.redis = redis_client
        self.hits: dict[tuple[str, int], int] = defaultdict(int)

    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/api/v1/") and request.url.path != "/api/v1/health" and not request.url.path.startswith("/api/v1/vision/frames/"):
            client = request.client.host if request.client else "unknown"
            window = math.floor(time.time() / 60)
            count = await self._count(client, window)
            if count > self.requests_per_minute:
                return JSONResponse(status_code=429, content={"detail": "rate limit exceeded"})
            response = await call_next(request)
            response.headers["X-RateLimit-Limit"] = str(self.requests_per_minute)
            response.headers["X-RateLimit-Remaining"] = str(max(0, self.requests_per_minute - count))
            return response
        return await call_next(request)

    async def _count(self, client: str, window: int) -> int:
        if self.redis is not None:
            try:
                key = f"edge-assistant:rate:{client}:{window}"
                count = await self.redis.incr(key)
                if count == 1:
                    await self.redis.expire(key, 120)
                return int(count)
            except Exception:
                logger.exception("Redis rate limiter unavailable; using process-local fallback")
        for key in tuple(self.hits):
            if key[1] < window:
                self.hits.pop(key, None)
        key = (client, window)
        self.hits[key] += 1
        return self.hits[key]


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or Settings.from_env()
    engine, db_factory = create_database(configured)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if configured.app_env == "production":
            if len(configured.auth_signing_secret) < 32:
                raise RuntimeError("production requires AUTH_SIGNING_SECRET with at least 32 characters")
            if configured.device_token and len(configured.device_token) < 32:
                raise RuntimeError("production requires DEVICE_TOKEN or registered per-device credentials")
            if not configured.allowed_origins or "*" in configured.allowed_origins:
                raise RuntimeError("production requires explicit ALLOWED_ORIGINS without wildcard")
            if not configured.allowed_hosts or "*" in configured.allowed_hosts:
                raise RuntimeError("production requires explicit ALLOWED_HOSTS without wildcard")
        Base.metadata.create_all(engine)
        if bool(configured.bootstrap_admin_username) != bool(configured.bootstrap_admin_password):
            raise RuntimeError("configure both BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD")
        if configured.bootstrap_admin_username:
            if len(configured.auth_signing_secret) < 32:
                raise RuntimeError("AUTH_SIGNING_SECRET must contain at least 32 characters")
            with db_factory() as db:
                if db.query(User).filter(User.username == configured.bootstrap_admin_username).first() is None:
                    db.add(User(
                        username=configured.bootstrap_admin_username,
                        password_hash=hash_password(configured.bootstrap_admin_password),
                        role="admin",
                    ))
                    db.commit()
        application.state.telemetry_broadcast_queue = asyncio.Queue(maxsize=256)
        try:
            await application.state.vision.start()
        except Exception:
            logging.getLogger(__name__).exception("Camera security could not start")
        worker = asyncio.create_task(scheduler_loop(application))
        automation_worker = asyncio.create_task(automation_rule_loop(application))
        telemetry_worker = asyncio.create_task(telemetry_broadcast_loop(application))
        voice_retention_worker = asyncio.create_task(voice_retention_loop(application))
        try:
            yield
        finally:
            await application.state.vision.stop()
            worker.cancel()
            automation_worker.cancel()
            telemetry_worker.cancel()
            voice_retention_worker.cancel()
            try:
                await asyncio.gather(worker, automation_worker, telemetry_worker, voice_retention_worker)
            except asyncio.CancelledError:
                pass
            if application.state.redis_client is not None:
                await application.state.redis_client.aclose()
            engine.dispose()

    application = FastAPI(title="Distributed Edge AI Assistant API", version="0.1.0", lifespan=lifespan)
    application.state.settings = configured
    application.state.engine = engine
    application.state.db_factory = db_factory
    application.state.devices = {}
    application.state.device_sockets = {}
    application.state.device_boot_greetings = {}
    application.state.pending_controls = []
    application.state.biometric_failures = {}
    application.state.telemetry_clients = set()
    application.state.telemetry_history = defaultdict(lambda: deque(maxlen=600))
    application.state.pending_pings = {}
    application.state.audio_echo_until = {}
    application.state.wake_briefings = {}
    application.state.game_waiters = {}
    application.state.device_capabilities = {}
    application.state.schedule_hashes = {}
    application.state.pending_setup = {}
    application.state.device_games = {}
    application.state.relisten = {}
    application.state.home_tasks = {}
    application.state.pending_suggestions = {}
    application.state.pending_todo_lists = {}
    application.state.pending_notes = {}
    application.state.pending_reminders = {}
    application.state.pending_alarms = {}
    application.state.pending_calendar_events = {}
    application.state.vision = VisionMonitor(application)
    application.state.ota_progress = {}
    application.state.ota_pending = {}
    application.state.ota_pending_lock = asyncio.Lock()
    redis_client = None
    if configured.redis_url:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:
            raise RuntimeError("REDIS_URL is configured but redis dependency is not installed") from exc
        redis_client = Redis.from_url(
            configured.redis_url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
    application.state.redis_client = redis_client
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(configured.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=list(configured.allowed_hosts))
    application.add_middleware(
        ApiRateLimitMiddleware,
        requests_per_minute=configured.rate_limit_per_minute,
        redis_client=redis_client,
    )
    application.include_router(auth_router, prefix="/api/v1")
    application.include_router(health_router, prefix="/api/v1")
    application.include_router(calendar_router, prefix="/api/v1")
    application.include_router(ai_router, prefix="/api/v1")
    application.include_router(audio_router, prefix="/api/v1")
    application.include_router(biometric_router, prefix="/api/v1")
    application.include_router(personalization_router, prefix="/api/v1")
    application.include_router(integrations_router, prefix="/api/v1")
    application.include_router(weather_router, prefix="/api/v1")
    application.include_router(news_router, prefix="/api/v1")
    application.include_router(brain_router, prefix="/api/v1")
    application.include_router(automation_router, prefix="/api/v1")
    application.include_router(games_router, prefix="/api/v1/games")
    application.include_router(audit_router, prefix="/api/v1")
    application.include_router(devices_router, prefix="/api/v1")
    application.include_router(monitoring_router, prefix="/api/v1")
    application.include_router(display_router, prefix="/api/v1")
    application.include_router(ota_router, prefix="/api/v1")
    application.include_router(alert_rules_router, prefix="/api/v1")
    application.include_router(vision_router, prefix="/api/v1")
    application.include_router(device_ws_router, prefix="/api/v1/ws")
    return application


app = create_app()

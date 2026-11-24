from __future__ import annotations

import hashlib
import logging
import math
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass

from sqlalchemy import select

from app.models import User, VoiceEnrollment
from app.services.biometric.anti_spoof import AntiSpoofUnavailable, bona_fide_score, trim_silence
from app.services.biometric.speaker import BiometricUnavailable, compare_voice

logger = logging.getLogger(__name__)

# Stores the most recent >= 3.0s WAV capture per device_id for enrollment
LAST_DEVICE_WAV: dict[str, tuple[float, bytes]] = {}
_CAPTURE_TTL_SECONDS = 600

# Active manual enrollment recordings: device_id -> (started_at_monotonic, bytearray_of_pcm16)
ACTIVE_RECORDINGS: dict[str, tuple[float, bytearray]] = {}
_MAX_RECORDING_BYTES = 16_000 * 2 * 20  # 20 seconds max at 16 kHz mono 16-bit


@dataclass(frozen=True)
class VerificationResult:
    accepted: bool
    reason: str
    confidence: float | None = None
    user_id: str | None = None


class ReplayGuard:
    def __init__(self, *, ttl_seconds: int = 600, max_entries: int = 4096) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()

    def accept(self, device_id: str, pcm: bytes, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        while self._seen:
            _, timestamp = next(iter(self._seen.items()))
            if timestamp > current - self.ttl_seconds:
                break
            self._seen.popitem(last=False)
        digest = hashlib.sha256(pcm).hexdigest()
        key = (device_id, digest)
        if key in self._seen:
            return False
        self._seen[key] = current
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)
        return True


def _quality(pcm: bytes) -> tuple[float, float]:
    if not pcm or len(pcm) % 2:
        raise ValueError("command audio must contain whole PCM16 samples")
    samples = struct.iter_unpack("<h", pcm)
    values = [sample[0] for sample in samples]
    rms = math.sqrt(sum(value * value for value in values) / len(values))
    clipped_ratio = sum(abs(value) >= 32760 for value in values) / len(values)
    return rms, clipped_ratio


def _cache_device_sample(device_id: str, pcm: bytes, duration: float) -> None:
    if 3.0 <= duration <= 20.0:
        trimmed = trim_silence(pcm, min_active_ms=1000)
        LAST_DEVICE_WAV[device_id] = (time.monotonic(), _pcm_to_wav(trimmed))
        logger.warning("Cached %.2fs live audio sample for device '%s' enrollment", duration, device_id)


def get_cached_device_wav(device_id: str) -> bytes | None:
    entry = LAST_DEVICE_WAV.get(device_id)
    if entry is None:
        return None
    captured_at, wav_bytes = entry
    if time.monotonic() - captured_at > _CAPTURE_TTL_SECONDS:
        LAST_DEVICE_WAV.pop(device_id, None)
        return None
    return wav_bytes


def start_device_recording(device_id: str) -> None:
    ACTIVE_RECORDINGS[device_id] = (time.monotonic(), bytearray())
    logger.warning("Started manual enrollment recording for device '%s'", device_id)


def append_device_recording(device_id: str, pcm_chunk: bytes) -> bool:
    """Appends incoming PCM16 frames if device is in recording mode. Returns True if captured."""
    entry = ACTIVE_RECORDINGS.get(device_id)
    if entry is None:
        return False
    started_at, buf = entry
    if time.monotonic() - started_at > 30.0:
        ACTIVE_RECORDINGS.pop(device_id, None)
        return False
    remaining = _MAX_RECORDING_BYTES - len(buf)
    if remaining > 0:
        buf.extend(pcm_chunk[:remaining])
    return True


def finish_device_recording(device_id: str) -> bytes:
    entry = ACTIVE_RECORDINGS.pop(device_id, None)
    if entry is None:
        raise ValueError(f"No active recording session for device '{device_id}'")
    _, buf = entry
    raw_pcm = bytes(buf)
    raw_duration = len(raw_pcm) / (16_000 * 2)
    trimmed_pcm = trim_silence(raw_pcm, min_active_ms=2000)
    trimmed_duration = len(trimmed_pcm) / (16_000 * 2)
    logger.warning(
        "Finished manual enrollment recording for device '%s': raw=%.2fs trimmed=%.2fs",
        device_id,
        raw_duration,
        trimmed_duration,
    )
    if trimmed_duration < 3.0:
        raise ValueError(
            f"Active speech too short ({trimmed_duration:.1f}s after trimming silence). Speak continuously for at least 4 seconds."
        )
    return _pcm_to_wav(trimmed_pcm)


def cancel_device_recording(device_id: str) -> None:
    ACTIVE_RECORDINGS.pop(device_id, None)


def verify_command_audio(
    db_factory,
    *,
    device_id: str,
    pcm: bytes,
    threshold: float,
    replay_guard: ReplayGuard,
    anti_spoof_model_path: str,
    anti_spoof_threshold: float,
    require_enrollment: bool = True,
) -> VerificationResult:
    duration = len(pcm) / (16_000 * 2)
    if not require_enrollment:
        with db_factory() as db:
            enrolled = db.execute(select(VoiceEnrollment.user_id).limit(1)).first() is not None
            primary = None if enrolled else db.execute(
                select(User.id).where(User.active.is_(True)).order_by(User.created_at).limit(1)
            ).scalar_one_or_none()
        if not enrolled:
            if not 0.5 <= duration <= 20:
                logger.warning("Biometric rejected (unenrolled mode): duration=%.2fs", duration)
                return VerificationResult(False, "duration_out_of_range")
            rms, clipped_ratio = _quality(pcm)
            if rms < 50 or rms > 24_000 or clipped_ratio > 0.05:
                logger.warning("Biometric rejected (unenrolled mode): rms=%.1f clipped=%.3f", rms, clipped_ratio)
                return VerificationResult(False, "audio_quality_rejected")
            if primary is None:
                return VerificationResult(False, "no_user_account")
            if not replay_guard.accept(device_id, pcm):
                return VerificationResult(False, "replayed_audio")
            _cache_device_sample(device_id, pcm, duration)
            return VerificationResult(True, "enrollment_not_required", None, primary)

    if not 0.2 <= duration <= 20:
        logger.warning("Biometric rejected: duration_out_of_range (%.2fs)", duration)
        return VerificationResult(False, "duration_out_of_range")

    rms, clipped_ratio = _quality(pcm)
    if rms < 50 or rms > 24_000 or clipped_ratio > 0.05:
        logger.warning("Biometric rejected: audio_quality_rejected (rms=%.1f clipped=%.3f)", rms, clipped_ratio)
        return VerificationResult(False, "audio_quality_rejected")

    if not replay_guard.accept(device_id, pcm):
        logger.warning("Biometric rejected: replayed_audio")
        return VerificationResult(False, "replayed_audio")

    _cache_device_sample(device_id, pcm, duration)

    try:
        liveness_score = bona_fide_score(pcm, anti_spoof_model_path)
    except AntiSpoofUnavailable as exc:
        logger.warning("Biometric rejected: anti_spoof_service_unavailable (%s)", exc)
        return VerificationResult(False, "anti_spoof_service_unavailable")

    if liveness_score < anti_spoof_threshold:
        logger.warning(
            "Biometric rejected: presentation_attack_suspected (liveness=%.4f < %.4f)",
            liveness_score,
            anti_spoof_threshold,
        )
        return VerificationResult(False, "presentation_attack_suspected", liveness_score)

    with db_factory() as db:
        enrollments = db.execute(select(VoiceEnrollment)).scalars().all()
        candidates = [(row.user_id, row.encrypted_embedding) for row in enrollments]
    if not candidates:
        logger.warning("Biometric rejected: no_voice_enrollment")
        return VerificationResult(False, "no_voice_enrollment")

    # Trim leading/trailing silence before speaker embedding extraction so pauses don't dilute similarity
    speech_pcm = trim_silence(pcm, min_active_ms=500)

    best_user: str | None = None
    best_score = float("-inf")
    try:
        wav_bytes = _pcm_to_wav(speech_pcm)
        for user_id, encrypted_embedding in candidates:
            score = compare_voice(wav_bytes, encrypted_embedding)
            if score > best_score:
                best_user = user_id
                best_score = score
    except BiometricUnavailable as exc:
        logger.warning("Biometric rejected: biometric_service_unavailable (%s)", exc)
        return VerificationResult(False, "biometric_service_unavailable")
    except ValueError as exc:
        logger.warning("Biometric rejected: compare_voice ValueError (duration=%.2fs rms=%.1f): %s", duration, rms, exc)
        return VerificationResult(False, "audio_quality_rejected")

    if best_user is None or best_score < threshold:
        logger.warning(
            "Biometric rejected: speaker_not_verified (best_score=%.4f < threshold=%.4f, duration=%.2fs, rms=%.1f)",
            best_score,
            threshold,
            duration,
            rms,
        )
        return VerificationResult(False, "speaker_not_verified", best_score)

    logger.info(
        "Biometric accepted: user=%s similarity=%.4f threshold=%.4f liveness=%.4f duration=%.2fs",
        best_user,
        best_score,
        threshold,
        liveness_score,
        duration,
    )
    return VerificationResult(True, "verified", best_score, best_user)


def _pcm_to_wav(pcm: bytes) -> bytes:
    from app.services.stt.local_whisper import pcm16le_to_wav

    return pcm16le_to_wav(pcm)

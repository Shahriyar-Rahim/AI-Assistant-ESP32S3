from __future__ import annotations

import hashlib
import math
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass

from sqlalchemy import select

from app.models import VoiceEnrollment
from app.services.biometric.anti_spoof import AntiSpoofUnavailable, bona_fide_score
from app.services.biometric.speaker import BiometricUnavailable, compare_voice


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


def verify_command_audio(
    db_factory,
    *,
    device_id: str,
    pcm: bytes,
    threshold: float,
    replay_guard: ReplayGuard,
    anti_spoof_model_path: str,
    anti_spoof_threshold: float,
) -> VerificationResult:
    duration = len(pcm) / (16_000 * 2)
    if not 3 <= duration <= 20:
        return VerificationResult(False, "duration_out_of_range")
    rms, clipped_ratio = _quality(pcm)
    if rms < 100 or rms > 24_000 or clipped_ratio > 0.05:
        return VerificationResult(False, "audio_quality_rejected")
    if not replay_guard.accept(device_id, pcm):
        return VerificationResult(False, "replayed_audio")
    try:
        liveness_score = bona_fide_score(pcm, anti_spoof_model_path)
    except AntiSpoofUnavailable:
        return VerificationResult(False, "anti_spoof_service_unavailable")
    if liveness_score < anti_spoof_threshold:
        return VerificationResult(False, "presentation_attack_suspected", liveness_score)

    with db_factory() as db:
        enrollments = db.execute(select(VoiceEnrollment)).scalars().all()
        candidates = [(row.user_id, row.encrypted_embedding) for row in enrollments]
    if not candidates:
        return VerificationResult(False, "no_voice_enrollment")

    best_user: str | None = None
    best_score = float("-inf")
    try:
        for user_id, encrypted_embedding in candidates:
            score = compare_voice(_pcm_to_wav(pcm), encrypted_embedding)
            if score > best_score:
                best_user = user_id
                best_score = score
    except BiometricUnavailable:
        return VerificationResult(False, "biometric_service_unavailable")
    except ValueError:
        return VerificationResult(False, "audio_quality_rejected")
    if best_user is None or best_score < threshold:
        return VerificationResult(False, "speaker_not_verified", best_score)
    return VerificationResult(True, "verified", best_score, best_user)


def _pcm_to_wav(pcm: bytes) -> bytes:
    from app.services.stt.local_whisper import pcm16le_to_wav

    return pcm16le_to_wav(pcm)

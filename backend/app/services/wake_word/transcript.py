from __future__ import annotations

import asyncio
import difflib
import os
import re

from app.services.stt.local_whisper import LocalSpeechUnavailable, _load_model, pcm16le_to_wav

GREETINGS = {"hey", "hay", "hi", "hei", "eh", "a", "hey,", "okay", "ok", "hello", "yo"}


def _similar(word: str, target: str) -> float:
    return difflib.SequenceMatcher(None, word, target).ratio()


def matches_wake_phrase(transcript: str, wake_phrase: str = "hey buddy") -> bool:
    """Fuzzy-match a Whisper transcript against the wake phrase, tolerating common mis-hearings."""
    words = re.findall(r"[a-z']+", transcript.casefold())
    phrase_words = re.findall(r"[a-z']+", wake_phrase.casefold())
    if not words or not phrase_words:
        return False
    target = phrase_words[-1]
    fused = "".join(phrase_words)
    for index, word in enumerate(words):
        if _similar(word, fused) >= 0.8:
            return True
        if index > 0 and words[index - 1] in {"hey", "hay", "hi"} and _similar(word, target) >= 0.55:
            return True
        if _similar(word, target) >= 0.75:
            if index == 0 or words[index - 1] in GREETINGS or _similar(words[index - 1], phrase_words[0]) >= 0.6:
                return True
            if index == 0:
                return True
    return False


def transcribe_wake_segment_sync(pcm: bytes, wake_phrase: str) -> str:
    pcm16le_to_wav(pcm)
    import numpy as np

    waveform = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    peak = float(np.max(np.abs(waveform))) if waveform.size else 0.0
    if peak < 1e-4:
        return ""
    waveform = np.clip(waveform * min(0.7 / peak, 8.0), -1.0, 1.0)
    model = _load_model(os.getenv("WAKE_WHISPER_MODEL", "base.en"), os.getenv("LOCAL_WHISPER_DEVICE", "cpu"),
                        os.getenv("LOCAL_WHISPER_COMPUTE_TYPE", "int8"))
    segments, _ = model.transcribe(
        waveform,
        language="en",
        beam_size=1,
        vad_filter=False,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
    )
    # Biasing with the phrase or keeping low-confidence segments makes Whisper hallucinate it from noise.
    return " ".join(
        segment.text.strip()
        for segment in segments
        if segment.no_speech_prob < 0.5 and segment.avg_logprob > -1.0
    ).strip()


async def spot_wake_phrase(pcm: bytes, wake_phrase: str) -> tuple[bool, str]:
    try:
        transcript = await asyncio.to_thread(transcribe_wake_segment_sync, pcm, wake_phrase)
    except LocalSpeechUnavailable:
        raise
    return matches_wake_phrase(transcript, wake_phrase), transcript


def transcript_wake_enabled() -> bool:
    return os.getenv("WAKE_TRANSCRIPT_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}

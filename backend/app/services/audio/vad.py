from dataclasses import dataclass


@dataclass
class VadGate:
    threshold: int
    end_silence_ms: int
    active: bool = False
    silence_ms: int = 0

    def update(self, rms: int, duration_ms: int) -> str | None:
        if duration_ms <= 0:
            raise ValueError("duration_ms must be positive")
        if rms >= self.threshold:
            self.silence_ms = 0
            if not self.active:
                self.active = True
                return "speech_started"
            return None
        if not self.active:
            return None
        self.silence_ms += duration_ms
        if self.silence_ms >= self.end_silence_ms:
            self.active = False
            self.silence_ms = 0
            return "speech_ended"
        return None

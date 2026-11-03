from dataclasses import dataclass


@dataclass
class VadGate:
    threshold: int
    end_silence_ms: int
    active: bool = False
    silence_ms: int = 0
    adaptive: bool = False
    noise_floor: float = 0.0

    def reset(self) -> None:
        self.active = False
        self.silence_ms = 0

    def _start_threshold(self) -> float:
        if not self.adaptive:
            return float(self.threshold)
        return max(float(self.threshold), self.noise_floor * 1.6 + 150.0)

    def _track_noise(self, rms: int, duration_ms: int) -> None:
        # Rise slowly and fall quickly so speech does not lift the floor but a quieter room is followed.
        if not self.adaptive:
            return
        if self.noise_floor <= 0.0:
            self.noise_floor = float(self.threshold)
        weight = min(1.0, duration_ms / (2000.0 if rms > self.noise_floor else 800.0))
        self.noise_floor += (rms - self.noise_floor) * weight

    def update(self, rms: int, duration_ms: int) -> str | None:
        if duration_ms <= 0:
            raise ValueError("duration_ms must be positive")
        self._track_noise(rms, duration_ms)
        start = self._start_threshold()
        keep = start * 0.6 if self.adaptive else start
        if not self.active and rms >= start or self.active and rms >= keep:
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

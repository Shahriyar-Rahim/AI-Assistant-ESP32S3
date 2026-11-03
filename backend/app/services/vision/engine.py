"""Local computer vision: face detection and embeddings (OpenCV YuNet + SFace), object memory (ORB) and OCR.

Only compact numeric descriptors are produced for matching; whole pictures are never needed to recognise a face or object.
"""

from __future__ import annotations

import gc
import io
import logging
import time
import threading
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "vision"
FACE_MATCH_THRESHOLD = 0.363  # SFace cosine similarity published by OpenCV for "same person"
FACE_MIN_SCORE = 0.85
OBJECT_MIN_GOOD_MATCHES = 18


class FaceEngine:
    """Two-stage pipeline: the tiny YuNet detector runs always; the 38 MB SFace recognizer is loaded only
    while a face is in view and released after `idle_release_seconds` without one (low-memory mode)."""

    def __init__(self, idle_release_seconds: float = 30.0, low_memory: bool = True) -> None:
        self._detector = None
        self._recognizer = None
        self._lock = threading.Lock()
        self.idle_release_seconds = idle_release_seconds
        self.low_memory = low_memory
        self._last_face = 0.0

    @property
    def recognizer_loaded(self) -> bool:
        return self._recognizer is not None

    def _load_detector(self) -> None:
        if self._detector is None:
            self._detector = cv2.FaceDetectorYN.create(str(MODEL_DIR / "yunet.onnx"), "", (320, 320), FACE_MIN_SCORE, 0.3, 5000)

    def _load_recognizer(self) -> None:
        if self._recognizer is None:
            self._recognizer = cv2.FaceRecognizerSF.create(str(MODEL_DIR / "sface.onnx"), "")

    def release(self) -> None:
        with self._lock:
            self._release_locked()

    def _release_locked(self) -> None:
        if self._recognizer is not None:
            self._recognizer = None
            gc.collect()
            logger.info("Face recognizer released (no face in view)")

    def faces(self, frame: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
        """Returns (detection row, normalised 128-d embedding) for every face found, largest first."""
        with self._lock:
            self._load_detector()
            height, width = frame.shape[:2]
            self._detector.setInputSize((width, height))
            _, found = self._detector.detect(frame)
            now = time.monotonic()
            if found is None:
                if self.low_memory and self._recognizer is not None and now - self._last_face > self.idle_release_seconds:
                    self._release_locked()
                return []
            self._last_face = now
            self._load_recognizer()
            result = []
            for row in sorted(found, key=lambda item: item[2] * item[3], reverse=True):
                aligned = self._recognizer.alignCrop(frame, row)
                vector = self._recognizer.feature(aligned).reshape(-1).astype(np.float32)
                norm = float(np.linalg.norm(vector))
                if norm > 0:
                    result.append((row, vector / norm))
            return result


class FaceGallery:
    """In-memory matrix of every stored embedding (no size cap) for vectorised matching."""

    def __init__(self) -> None:
        self._vectors = np.zeros((0, 128), dtype=np.float32)
        self._owners: list[str] = []
        self._lock = threading.Lock()

    def load(self, pairs: list[tuple[str, bytes]]) -> None:
        with self._lock:
            self._owners = [identity for identity, _ in pairs]
            self._vectors = (
                np.vstack([np.frombuffer(blob, dtype=np.float32) for _, blob in pairs])
                if pairs else np.zeros((0, 128), dtype=np.float32)
            )

    def add(self, identity_id: str, vector: np.ndarray) -> None:
        with self._lock:
            self._vectors = np.vstack([self._vectors, vector.astype(np.float32).reshape(1, -1)])
            self._owners.append(identity_id)

    def match(self, vector: np.ndarray, allowed: set[str] | None = None) -> tuple[str | None, float]:
        with self._lock:
            if not self._owners:
                return None, 0.0
            scores = self._vectors @ vector
            order = np.argsort(scores)[::-1]
            for index in order:
                owner = self._owners[int(index)]
                if allowed is None or owner in allowed:
                    score = float(scores[index])
                    return (owner if score >= FACE_MATCH_THRESHOLD else None), score
            return None, 0.0

    def remove(self, identity_id: str) -> None:
        with self._lock:
            keep = [index for index, owner in enumerate(self._owners) if owner != identity_id]
            self._vectors = self._vectors[keep] if keep else np.zeros((0, 128), dtype=np.float32)
            self._owners = [self._owners[index] for index in keep]

    def count(self) -> int:
        return len(self._owners)


def _centre_crop(frame: np.ndarray) -> np.ndarray:
    height, width = frame.shape[:2]
    return frame[int(height * 0.15):int(height * 0.85), int(width * 0.2):int(width * 0.8)]


def describe_object(frame: np.ndarray) -> bytes:
    """ORB descriptors of the centre of the frame, where the user holds the object."""
    gray = cv2.cvtColor(_centre_crop(frame), cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (480, int(480 * gray.shape[0] / gray.shape[1])))
    _, descriptors = cv2.ORB_create(nfeatures=600).detectAndCompute(gray, None)
    if descriptors is None or len(descriptors) < OBJECT_MIN_GOOD_MATCHES:
        raise ValueError("not enough visual detail to remember this object; hold it closer with good light")
    buffer = io.BytesIO()
    np.save(buffer, descriptors, allow_pickle=False)
    return buffer.getvalue()


def match_object(frame: np.ndarray, memories: list[tuple[str, bytes]]) -> tuple[str | None, int]:
    try:
        current = np.load(io.BytesIO(describe_object(frame)), allow_pickle=False)
    except ValueError:
        return None, 0
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    best_id, best_count = None, 0
    for memory_id, blob in memories:
        stored = np.load(io.BytesIO(blob), allow_pickle=False)
        good = 0
        for pair in matcher.knnMatch(stored, current, k=2):
            if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
                good += 1
        if good > best_count:
            best_id, best_count = memory_id, good
    return (best_id if best_count >= OBJECT_MIN_GOOD_MATCHES else None), best_count


_ocr = None
_ocr_lock = threading.Lock()


def read_text(frame: np.ndarray, release_after: bool = True) -> str:
    """The OCR models are loaded for one read and then freed so they do not stay resident."""
    global _ocr
    with _ocr_lock:
        if _ocr is None:
            from rapidocr import RapidOCR
            _ocr = RapidOCR()
        try:
            output = _ocr(frame)
        finally:
            if release_after:
                _ocr = None
                gc.collect()
    texts = getattr(output, "txts", None) or ()
    return "\n".join(str(item).strip() for item in texts if str(item).strip())


def encode_jpeg(frame: np.ndarray, quality: int = 88) -> bytes:
    ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("could not encode frame")
    return buffer.tobytes()


def decode_jpeg(data: bytes) -> np.ndarray | None:
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)

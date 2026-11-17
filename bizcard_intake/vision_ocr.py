"""Apple Vision OCR wrapper."""

from __future__ import annotations

import json
import logging
import math
import subprocess
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VISION_OCR_SOURCE = PROJECT_ROOT / "scripts" / "vision_ocr.swift"
VISION_OCR_BINARY = PROJECT_ROOT / ".cache" / "vision_ocr"

log = logging.getLogger("bizcard")


@contextmanager
def stage_timer(stage: str):
    """Record elapsed wall time without including card contents or credentials."""
    started = time.perf_counter()
    try:
        yield
    finally:
        log.info("perf stage=%s seconds=%.6f", stage, time.perf_counter() - started)


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    top_left: tuple[float, float] | None = None
    top_right: tuple[float, float] | None = None


@dataclass(frozen=True)
class OcrResult:
    lines: tuple[OcrLine, ...]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def average_confidence(self) -> float:
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)


def vote_orientation(lines: Iterable[OcrLine], width: int, height: int) -> tuple[int, float, float]:
    """Return clockwise correction quarters, winning share and character weight.

    Vision coordinates start at the bottom left. Scale the line vector to image
    pixels before rounding its angle to the nearest quarter turn. Missing
    coordinates (older helpers), single characters and zero vectors cannot vote.
    """
    votes = [0.0] * 4
    for line in lines:
        if len(line.text) < 2 or line.top_left is None or line.top_right is None:
            continue
        dx = (line.top_right[0] - line.top_left[0]) * width
        dy = (line.top_right[1] - line.top_left[1]) * height
        weight = len(line.text) * line.confidence
        if not all(math.isfinite(value) for value in (dx, dy, weight)) or weight <= 0 or (dx == 0 and dy == 0):
            continue
        quarters = int((math.degrees(math.atan2(dy, dx)) + 45) % 360 // 90)
        votes[quarters] += weight
    total = sum(votes)
    best = max(range(4), key=votes.__getitem__)
    return best, votes[best] / total if total else 0.0, total


def _point(value) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        point = float(value["x"]), float(value["y"])
    except (KeyError, TypeError, ValueError):
        return None
    return point if all(math.isfinite(component) for component in point) else None


def extract_text(image_path: Path) -> OcrResult:
    binary = _ensure_vision_ocr()
    if binary is None:
        return OcrResult(())

    try:
        completed = subprocess.run(
            [str(binary), str(image_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except Exception:
        log.exception("vision_ocr subprocess failed")
        return OcrResult(())

    if completed.returncode != 0:
        log.warning("vision_ocr failed: %s", completed.stderr.strip())
        return OcrResult(())

    try:
        rows = json.loads(completed.stdout)
    except json.JSONDecodeError:
        log.warning("vision_ocr returned non-json output")
        return OcrResult(())

    lines = tuple(
        OcrLine(
            str(row.get("text", "")).strip(), float(row.get("confidence", 0.0)),
            _point(row.get("topLeft")), _point(row.get("topRight")),
        )
        for row in rows
        if str(row.get("text", "")).strip()
    )
    return OcrResult(lines)


_helper_build_lock = threading.Lock()


def _ensure_vision_ocr() -> Path | None:
    with _helper_build_lock:
        # Recheck after acquiring the lock: another worker may have rebuilt it.
        if VISION_OCR_BINARY.exists() and (
            not VISION_OCR_SOURCE.exists()
            or VISION_OCR_BINARY.stat().st_mtime_ns >= VISION_OCR_SOURCE.stat().st_mtime_ns
        ):
            return VISION_OCR_BINARY
        if not VISION_OCR_SOURCE.exists():
            return None
        VISION_OCR_BINARY.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="._ensure_vision_ocr.", dir=VISION_OCR_BINARY.parent) as folder:
            temporary = Path(folder) / VISION_OCR_BINARY.name
            try:
                result = subprocess.run(
                    ["swiftc", str(VISION_OCR_SOURCE), "-O", "-o", str(temporary)],
                    check=False, capture_output=True, text=True, timeout=20,
                )
                if result.returncode != 0 or not temporary.exists():
                    log.warning("swiftc _ensure_vision_ocr build failed")
                    return None
                os.replace(temporary, VISION_OCR_BINARY)
            except Exception:
                log.exception("swiftc _ensure_vision_ocr build failed")
                return None
        return VISION_OCR_BINARY

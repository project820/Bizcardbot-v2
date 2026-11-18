"""Split one photo that holds several business cards into one crop per card.

Measured on 2026-11-18 with synthetic photos of Korean sample cards in random
poses: OCR quality stops improving once a card's long side reaches about 500 px,
and a Telegram HD photo (2560 px) leaves at least that much for up to four cards.
Anything we are unsure about falls back to the existing one-card path.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HELPER_SOURCE = PROJECT_ROOT / "scripts" / "vision_cards.swift"
HELPER_BINARY = PROJECT_ROOT / ".cache" / "vision_cards"

MAX_CARDS = 4
MIN_LONG_SIDE = 500  # pixels per card, see module docstring
MIN_TEXT_CHARS = 20   # empty table patches, shadows and logos read as 0 characters
SIDE_RATIO = 0.75     # opposite sides of a card stay within 25% even with camera tilt
ASPECT = (1.25, 2.10)
OVERLAP = 0.5         # a candidate half inside a larger one is a logo or a duplicate

log = logging.getLogger("bizcard")


@dataclass(frozen=True)
class Split:
    kind: str                      # single | split | too_many | too_small
    crops: tuple[Path, ...] = ()
    count: int = 0


def enabled() -> bool:
    return os.environ.get("BIZCARD_MULTICARD", "on").strip().lower() not in ("0", "off", "false", "no")


def detect(source: Path, work_dir: Path) -> Split:
    """Return per-card crops only when two to four readable cards are certain."""
    if not enabled():
        return Split("single")
    try:
        from PIL import Image
        with Image.open(source) as image:
            image.verify()
    except Exception:
        return Split("single")  # HEIC or broken files keep today's path
    result = None
    try:
        output = _run_helper(source, work_dir)
        if output:
            result = _decide(output)
    except Exception:
        log.exception("multicard detection failed; using the whole photo")
    if result is None or result.kind != "split":
        shutil.rmtree(work_dir, ignore_errors=True)
        return result if result is not None else Split("single")
    return result


def _decide(output: dict) -> Split:
    width, height = output["image_width"], output["image_height"]
    candidates = select(output["rectangles"], width, height)
    if len(candidates) < 2:
        return Split("single")
    counts = [_text_chars(Path(r["crop_path"])) for r in candidates]
    if any(count is None for count in counts):
        return Split("single")  # an unread candidate may be a real card; never drop it
    readable = [r for r, count in zip(candidates, counts) if count >= MIN_TEXT_CHARS]
    if len(readable) < 2:
        return Split("single")
    if len(readable) > MAX_CARDS:
        return Split("too_many", count=len(readable))
    if any(max(_sides(r["corners"], width, height)) < MIN_LONG_SIDE for r in readable):
        return Split("too_small", count=len(readable))
    order = {r["index"]: i for i, r in enumerate(output["rectangles"])}
    readable.sort(key=lambda r: order[r["index"]])
    return Split("split", tuple(Path(r["crop_path"]) for r in readable), len(readable))


def _sides(corners, width, height) -> list[float]:
    points = [(x * width, y * height) for x, y in corners]
    return [math.dist(points[i], points[(i + 1) % 4]) for i in range(4)]


def _card_shaped(corners, width, height) -> bool:
    top, right, bottom, left = _sides(corners, width, height)
    if min(top, right, bottom, left) <= 0:
        return False
    if min(top, bottom) / max(top, bottom) < SIDE_RATIO or min(left, right) / max(left, right) < SIDE_RATIO:
        return False
    long_side, short_side = sorted(((top + bottom) / 2, (left + right) / 2), reverse=True)
    return ASPECT[0] <= long_side / short_side <= ASPECT[1]


def _area(corners, width, height) -> float:
    points = [(x * width, y * height) for x, y in corners]
    return abs(sum(points[i][0] * points[(i + 1) % 4][1] - points[(i + 1) % 4][0] * points[i][1] for i in range(4))) / 2


def _overlap_of_smaller(a, b, width, height) -> float:
    """Share of the smaller quad covered by the larger one, sampled on a grid."""
    from PIL import Image, ImageChops, ImageDraw
    scale = 512 / max(width, height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    masks = []
    for corners in (a, b):
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).polygon([(x * size[0], y * size[1]) for x, y in corners], fill=255)
        masks.append(mask)
    both = ImageChops.darker(*masks).histogram()[255]
    smaller = min(m.histogram()[255] for m in masks)
    return both / smaller if smaller else 1.0


def select(rectangles, width, height) -> list[dict]:
    """Keep card-shaped quads, largest first, dropping ones mostly inside a kept quad."""
    shaped = [r for r in rectangles if _card_shaped(r["corners"], width, height)]
    shaped.sort(key=lambda r: -_area(r["corners"], width, height))
    kept: list[dict] = []
    for rect in shaped:
        if any(_overlap_of_smaller(rect["corners"], k["corners"], width, height) >= OVERLAP for k in kept):
            continue
        kept.append(rect)
    return kept


def _text_chars(path: Path) -> Optional[int]:
    """Non-space characters on the crop, or None when OCR itself failed."""
    from .vision_ocr import extract_text
    result = extract_text(path)
    if result.failed:
        return None
    return sum(not c.isspace() for c in result.text)


def _run_helper(source: Path, work_dir: Path) -> Optional[dict]:
    binary = _ensure_helper()
    if binary is None:
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run([str(binary), str(source), str(work_dir)],
                               check=False, capture_output=True, text=True, timeout=20)
    if completed.returncode != 0:
        log.warning("vision_cards failed: %s", completed.stderr.strip()[:200])
        return None
    return json.loads(completed.stdout)


_build_lock = threading.Lock()


def _ensure_helper() -> Optional[Path]:
    with _build_lock:
        if HELPER_BINARY.exists() and (
            not HELPER_SOURCE.exists()
            or HELPER_BINARY.stat().st_mtime_ns >= HELPER_SOURCE.stat().st_mtime_ns
        ):
            return HELPER_BINARY
        if not HELPER_SOURCE.exists():
            return None
        HELPER_BINARY.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="._ensure_vision_cards.", dir=HELPER_BINARY.parent) as folder:
            temporary = Path(folder) / HELPER_BINARY.name
            try:
                result = subprocess.run(["swiftc", str(HELPER_SOURCE), "-O", "-o", str(temporary)],
                                        check=False, capture_output=True, text=True, timeout=60)
                if result.returncode != 0 or not temporary.exists():
                    log.warning("swiftc vision_cards build failed")
                    return None
                os.replace(temporary, HELPER_BINARY)
            except Exception:
                log.exception("swiftc vision_cards build failed")
                return None
        return HELPER_BINARY

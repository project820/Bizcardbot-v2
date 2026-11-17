#!/usr/bin/env python3
"""Evaluate local Vision orientation on 0/90/180/270° clockwise input turns.

Prefer *_rectified.jpg fixtures so a second rectification cannot change their
orientation. The known file_24/file_26 fixtures need 270°/180° clockwise
correction before any additional input turn; other files are labeled upright.
Images and recognized text never leave this machine. Only aggregate direction
and timing metrics are printed; temporary images are removed on exit.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageOps

from bizcard_intake.image_processing import PreparedImages, prepare_card_images
from bizcard_intake.pipeline import ORIENTATION_MIN_SHARE, ORIENTATION_MIN_TOTAL
from bizcard_intake.vision_ocr import _ensure_vision_ocr, extract_text, vote_orientation


def image_paths(folder: Path) -> list[Path]:
    paths = sorted(path for path in folder.iterdir() if path.is_file() and path.suffix.lower() == ".jpg")
    rectified = [path for path in paths if path.stem.endswith("_rectified")]
    return rectified or paths


def expected_correction(path: Path, input_quarters: int) -> int:
    match = re.match(r"file_(\d+)(?:_|$)", path.stem)
    base = {"24": 3, "26": 2}.get(match.group(1) if match else "", 0)
    return (base - input_quarters) % 4


def evaluate(folder: Path) -> int:
    paths = image_paths(folder)
    if not paths:
        raise SystemExit(f"No JPG images in {folder}")
    if _ensure_vision_ocr() is None:
        raise SystemExit("Vision OCR helper is unavailable")
    correct = verified = count = 0
    minimum_share = 1.0
    minimum_total = float("inf")
    seconds = []
    print(f"images={len(paths)} rotations=0/90/180/270 clockwise usesLanguageCorrection=true", flush=True)
    print(f"thresholds: total>={ORIENTATION_MIN_TOTAL} share>={ORIENTATION_MIN_SHARE}", flush=True)
    with tempfile.TemporaryDirectory(prefix="bizcard-orientation-eval-") as tmp:
        root = Path(tmp)
        for path in paths:
            file_correct = file_verified = 0
            file_share = 1.0
            file_seconds = []
            with Image.open(path) as original:
                original = ImageOps.exif_transpose(original).convert("RGB")
                for input_quarters in range(4):
                    case = root / f"{path.stem}_{input_quarters}"
                    source = case / "input" / "card.jpg"
                    source.parent.mkdir(parents=True)
                    original.rotate(-90 * input_quarters, expand=True).save(source, quality=95)
                    started = time.monotonic()
                    # Use the exact production contrast/sharpen/1600px path,
                    # treating this already-rectified fixture as the first pass.
                    reuse = PreparedImages(source, source, source, None, "original")
                    profile = case / "profile" / "card.jpg"
                    prepared = prepare_card_images(source, profile, reuse=reuse)
                    ocr = extract_text(prepared.ocr_path)
                    with Image.open(prepared.ocr_path) as image:
                        quarters, share, total = vote_orientation(ocr.lines, image.width, image.height)
                    confident = total >= ORIENTATION_MIN_TOTAL and share >= ORIENTATION_MIN_SHARE
                    expected = expected_correction(path, input_quarters)
                    is_correct = confident and quarters == expected
                    upright = confident and quarters == 0
                    if confident and quarters:
                        prepared = prepare_card_images(source, profile, rotate_quarters=quarters, reuse=prepared)
                        checked = extract_text(prepared.ocr_path)
                        with Image.open(prepared.ocr_path) as image:
                            q, s, t = vote_orientation(checked.lines, image.width, image.height)
                        upright = q == 0 and s >= ORIENTATION_MIN_SHARE and t >= ORIENTATION_MIN_TOTAL
                    elapsed = time.monotonic() - started
                    seconds.append(elapsed)
                    file_seconds.append(elapsed)
                    count += 1
                    correct += is_correct
                    verified += is_correct and upright
                    file_correct += is_correct
                    file_verified += is_correct and upright
                    file_share = min(file_share, share)
                    minimum_share = min(minimum_share, share)
                    minimum_total = min(minimum_total, total)
                    if not is_correct or not upright:
                        print(f"FAIL {path.name} input={input_quarters * 90} expected={expected * 90} predicted={quarters * 90} share={share:.6f} total={total:.2f} upright_verified={upright}", flush=True)
            print(f"{path.name}: correct={file_correct}/4 verified={file_verified}/4 min_share={file_share:.6f} mean_seconds={sum(file_seconds) / 4:.3f}", flush=True)
    print(f"accuracy={correct}/{count} ({100 * correct / count:.2f}%)", flush=True)
    print(f"verified_upright={verified}/{count} ({100 * verified / count:.2f}%)", flush=True)
    print(f"minimum_share={minimum_share:.6f} minimum_total={minimum_total:.2f}", flush=True)
    print(f"seconds_per_image: mean={sum(seconds) / count:.3f} min={min(seconds):.3f} max={max(seconds):.3f} total={sum(seconds):.3f}", flush=True)
    return 0 if verified == count else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    return evaluate(args.folder)


if __name__ == "__main__":
    raise SystemExit(main())

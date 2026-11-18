"""Splitting one photo that holds several business cards."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from bizcard_intake import multicard


def corners(x, y, w, h):
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


class SelectTest(unittest.TestCase):
    def test_drops_inner_logo_duplicates_and_non_card_shapes(self):
        rects = [
            {"index": 0, "corners": corners(0.1, 0.1, 0.3, 0.25)},     # 1200x750 px card
            {"index": 1, "corners": corners(0.12, 0.12, 0.08, 0.07)},  # card-shaped logo inside card 0
            {"index": 2, "corners": corners(0.1, 0.5, 0.3, 0.25)},     # second card
            {"index": 3, "corners": corners(0.12, 0.52, 0.27, 0.22)},  # same card found again, slightly inside
            {"index": 4, "corners": corners(0.7, 0.1, 0.05, 0.6)},    # long strip, not a card
        ]
        kept = multicard.select(rects, 4000, 3000)
        self.assertEqual(sorted(r["index"] for r in kept), [0, 2])


class DetectTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.photo = self.tmp / "photo.jpg"
        Image.new("RGB", (4000, 3000), "gray").save(self.photo)
        self.addCleanup(self._tmp.cleanup)

    def helper_output(self, boxes):
        out = self.tmp / "work"
        out.mkdir(exist_ok=True)
        rects = []
        for i, box in enumerate(boxes):
            crop = out / f"candidate-{i:03d}.jpg"
            Image.new("RGB", (100, 60), "white").save(crop)
            rects.append({"index": i, "box": box, "confidence": 1.0, "crop_path": str(crop), "corners": corners(*box)})
        return {"image_width": 4000, "image_height": 3000, "rectangles": rects}

    def detect(self, boxes, chars=40, lines=()):
        with patch.object(multicard, "_run_helper", return_value=self.helper_output(boxes)) as run, \
             patch.object(multicard, "_text_chars", side_effect=chars if isinstance(chars, list) else lambda p: chars), \
             patch.object(multicard, "_text_lines", return_value=None if lines is None else list(lines)):
            return multicard.detect(self.photo, self.tmp / "work"), run

    def row(self, n, width=0.2):
        return [(0.05 + (i % 4) * 0.24, 0.1 + (i // 4) * 0.4, width, width * 0.8) for i in range(n)]

    def test_one_card_keeps_the_existing_single_path(self):
        result, _ = self.detect(self.row(1))
        self.assertEqual(result.kind, "single")
        self.assertEqual(result.crops, ())

    def test_two_to_four_cards_split_in_reading_order(self):
        for n in (2, 3, 4):
            with self.subTest(n=n):
                result, _ = self.detect(self.row(n))
                self.assertEqual(result.kind, "split")
                self.assertEqual(result.count, n)
                self.assertEqual(len(result.crops), n)
                self.assertTrue(all(Path(p).exists() for p in result.crops))

    def test_more_than_four_cards_asks_for_fewer(self):
        result, _ = self.detect(self.row(5))
        self.assertEqual((result.kind, result.count, result.crops), ("too_many", 5, ()))

    def test_small_cards_ask_for_hd_or_file(self):
        # 0.1 of 4000 px = 400 px long side, below the 500 px measurement threshold.
        result, _ = self.detect(self.row(3, width=0.1))
        self.assertEqual((result.kind, result.count), ("too_small", 3))

    def test_candidates_without_text_are_ignored(self):
        result, _ = self.detect(self.row(2), chars=[0, 40])
        self.assertEqual(result.kind, "single")

    def test_ocr_failure_on_any_candidate_keeps_the_whole_photo(self):
        result, _ = self.detect(self.row(3), chars=[40, 40, None])
        self.assertEqual((result.kind, result.crops), ("single", ()))
        self.assertFalse((self.tmp / "work").exists())

    def test_text_outside_the_found_cards_marks_a_possible_missing_card(self):
        inside = {"chars": 30, "center": [0.15, 0.2]}      # within the first card of row(2)
        outside = {"chars": 30, "center": [0.9, 0.9]}      # a card Vision did not find
        for lines, expected in (([inside], False), ([inside, outside], True), (None, True)):
            with self.subTest(lines=lines):
                result, _ = self.detect(self.row(2), lines=lines)
                self.assertEqual((result.kind, result.leftover), ("split", expected))

    def test_helper_failure_or_undecodable_image_keeps_single_path(self):
        with patch.object(multicard, "_run_helper", return_value=None):
            self.assertEqual(multicard.detect(self.photo, self.tmp / "work").kind, "single")
        bad = self.tmp / "bad.jpg"
        bad.write_bytes(b"jpg")
        with patch.object(multicard, "_run_helper", side_effect=AssertionError("must not run")):
            self.assertEqual(multicard.detect(bad, self.tmp / "work").kind, "single")

    def test_switch_off_keeps_single_path(self):
        with patch.dict(os.environ, {"BIZCARD_MULTICARD": "off"}), \
             patch.object(multicard, "_run_helper", side_effect=AssertionError("must not run")):
            self.assertEqual(multicard.detect(self.photo, self.tmp / "work").kind, "single")


if __name__ == "__main__":
    unittest.main()

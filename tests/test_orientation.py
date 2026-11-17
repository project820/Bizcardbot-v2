import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import image_processing, vision_ocr
from bizcard_intake.vision_ocr import OcrLine


class OrientationTest(unittest.TestCase):
    def line(self, text="abcdefghij", confidence=1.0, left=(0.1, 0.5), right=(0.9, 0.5)):
        return OcrLine(text, confidence, left, right)

    def test_vote_reports_clockwise_correction_for_four_directions(self):
        self.assertTrue(hasattr(vision_ocr, "vote_orientation"))
        for quarters, right in enumerate(((0.9, 0.5), (0.1, 0.9), (-0.7, 0.5), (0.1, 0.1))):
            with self.subTest(quarters=quarters):
                self.assertEqual(vision_ocr.vote_orientation([self.line(right=right)], 1600, 1000), (quarters, 1.0, 10.0))

    def test_vote_weights_character_count_and_confidence(self):
        self.assertTrue(hasattr(vision_ocr, "vote_orientation"))
        lines = [self.line("abcdefghij", 0.8), self.line("abcde", 0.4, right=(0.1, 0.9))]
        self.assertEqual(vision_ocr.vote_orientation(lines, 1600, 1000), (0, 0.8, 10.0))

    def test_vote_uses_pixel_aspect_ratio(self):
        self.assertTrue(hasattr(vision_ocr, "vote_orientation"))
        line = self.line(left=(0.1, 0.1), right=(0.5, 0.7))
        self.assertEqual(vision_ocr.vote_orientation([line], 1600, 400)[0], 0)
        self.assertEqual(vision_ocr.vote_orientation([line], 400, 1600)[0], 1)

    def test_vote_ignores_missing_short_and_degenerate_lines(self):
        self.assertTrue(hasattr(vision_ocr, "vote_orientation"))
        lines = [OcrLine("old output", 1), self.line("a"), self.line(left=(0.1, 0.5), right=(0.1, 0.5)), self.line(confidence=0)]
        self.assertEqual(vision_ocr.vote_orientation(lines, 1600, 1000), (0, 0.0, 0.0))
        self.assertEqual(vision_ocr.vote_orientation([self.line("ab")], 1600, 1000)[2], 2.0)

    def test_ocr_accepts_old_output_and_reads_new_coordinates(self):
        rows = [{"text": "old", "confidence": 0.8}, {"text": "new", "confidence": 0.9, "topLeft": {"x": 0.1, "y": 0.5}, "topRight": {"x": 0.9, "y": 0.5}}]
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(rows), stderr="")
        with patch.object(vision_ocr, "_ensure_vision_ocr", return_value=Path("helper")), patch.object(vision_ocr.subprocess, "run", return_value=completed) as run:
            result = vision_ocr.extract_text(Path("image.jpg"))
        self.assertTrue(hasattr(result.lines[0], "top_left"))
        self.assertIsNone(result.lines[0].top_left)
        self.assertEqual(result.lines[1].top_left, (0.1, 0.5))
        self.assertEqual(result.lines[1].top_right, (0.9, 0.5))
        self.assertEqual(run.call_args.kwargs["timeout"], 12)

    def test_swift_helpers_rebuild_when_source_is_newer(self):
        for module, source_name, binary_name, ensure_name in (
            (vision_ocr, "VISION_OCR_SOURCE", "VISION_OCR_BINARY", "_ensure_vision_ocr"),
            (image_processing, "VISION_RECTIFY_SOURCE", "VISION_RECTIFY_BINARY", "_ensure_vision_rectifier"),
        ):
            with self.subTest(helper=ensure_name), tempfile.TemporaryDirectory() as tmp:
                source, binary = Path(tmp) / "helper.swift", Path(tmp) / "helper"
                source.write_text("source")
                binary.write_text("old binary")
                os.utime(binary, (1, 1))
                def compile_helper(args, **kwargs):
                    Path(args[-1]).write_text("rebuilt binary")
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                with patch.object(module, source_name, source), patch.object(module, binary_name, binary), patch.object(module.subprocess, "run", side_effect=compile_helper) as run:
                    self.assertEqual(getattr(module, ensure_name)(), binary)
                    self.assertEqual(run.call_count, 1)
                    os.utime(binary, (source.stat().st_mtime + 1, source.stat().st_mtime + 1))
                    run.reset_mock()
                    self.assertEqual(getattr(module, ensure_name)(), binary)
                    run.assert_not_called()


class OrientationEvaluationTest(unittest.TestCase):
    def load_script(self):
        import runpy
        path = Path(__file__).resolve().parent.parent / "scripts" / "eval_orientation.py"
        self.assertTrue(path.exists(), "orientation evaluation CLI is available")
        return runpy.run_path(str(path))

    def test_expected_correction_accounts_for_original_and_added_rotation(self):
        script = self.load_script()
        expected = script["expected_correction"]
        self.assertEqual([expected(Path("file_24_profile_rectified.jpg"), q) for q in range(4)], [3, 2, 1, 0])
        self.assertEqual([expected(Path("file_26_profile_rectified.jpg"), q) for q in range(4)], [2, 1, 0, 3])
        self.assertEqual([expected(Path("card.jpg"), q) for q in range(4)], [0, 3, 2, 1])

    def test_evaluation_prefers_rectified_fixture_set(self):
        script = self.load_script()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ("file_24.jpg", "file_24_profile_rectified.jpg", "file_26_profile_rectified.jpg"):
                (folder / name).touch()
            self.assertEqual([p.name for p in script["image_paths"](folder)], ["file_24_profile_rectified.jpg", "file_26_profile_rectified.jpg"])


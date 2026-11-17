import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from bizcard_intake import image_processing


class ProfileWhiteBalanceTest(unittest.TestCase):
    def prepare(self, paper, background, quarters=0, rectify=True):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        card = Image.new("RGB", (400, 240), paper)
        card.paste((10, 10, 10), (20, 20, 80, 40))
        card.save(root / "card.png")
        frame = Image.new("RGB", (600, 400), background)
        frame.paste(card, (100, 80))
        frame.save(root / "frame.png")
        with patch.object(image_processing, "_try_vision_rectify", return_value=root / "card.png" if rectify else None), patch.object(image_processing, "_try_opencv_rectify", return_value=None):
            prepared = image_processing.prepare_card_images(
                root / "frame.png", root / "profile/card.jpg", rotate_quarters=quarters,
            )
        with Image.open(prepared.profile_path) as profile:
            pixel = profile.getpixel((512, 512))
        return pixel, prepared

    def test_light_paper_is_whitened_despite_different_background(self):
        for paper, background in [
            ((183, 234, 251), (160, 120, 85)),
            ((236, 233, 219), (60, 80, 90)),
            ((190, 190, 190), (210, 160, 120)),
        ]:
            with self.subTest(paper=paper):
                pixel, _ = self.prepare(paper, background)
                self.assertGreaterEqual(min(pixel), 235)
                self.assertLessEqual(max(pixel), 250)
                self.assertLess(max(pixel) - min(pixel), 15)

    def test_dark_or_saturated_paper_is_unchanged(self):
        for paper in [(70, 90, 100), (35, 100, 230), (250, 180, 180), (100, 220, 180)]:
            with self.subTest(paper=paper):
                pixel, _ = self.prepare(paper, (220, 220, 220))
                for actual, expected in zip(pixel, paper):
                    self.assertAlmostEqual(actual, expected, delta=2)

    def test_small_white_print_does_not_whiten_colored_stock(self):
        card = Image.new("RGB", (400, 240), (30, 80, 180))
        card.paste((230, 230, 230), (30, 30, 200, 60))
        result = image_processing._white_balance_profile(card)
        self.assertEqual(result.tobytes(), card.tobytes())

    def test_gain_is_limited_for_dim_paper(self):
        card = Image.new("RGB", (400, 240), (145, 155, 165))
        result = image_processing._white_balance_profile(card)
        for before, after in zip((145, 155, 165), result.getpixel((200, 120))):
            self.assertLessEqual(after, round(before * 1.35))
            self.assertGreater(after, before)

    def test_gain_limit_does_not_introduce_yellow_cast(self):
        card = Image.new("RGB", (400, 240), (211, 191, 166))
        result = image_processing._white_balance_profile(card)
        paper = result.getpixel((200, 120))
        self.assertLessEqual(max(paper) - min(paper), 12)
        self.assertGreaterEqual(min(paper), 220)

    def test_highlight_shoulders_preserve_logo_detail_without_new_clipping(self):
        card = Image.new("RGB", (400, 240), (183, 234, 251))
        colors = [(230, 30, 60), (250, 80, 30), (20, 210, 60), (20, 40, 250), (10, 10, 10)]
        for index, color in enumerate(colors):
            card.paste(color, (20 + index * 60, 40, 60 + index * 60, 80))
        result = image_processing._white_balance_profile(card)
        for index, color in enumerate(colors):
            actual = result.getpixel((40 + index * 60, 60))
            self.assertLess(max(actual), 255)
            self.assertLessEqual(max(actual), max(color) * 1.35 + 1)
            self.assertLessEqual(min(actual), min(color) * 1.35 + 1)
            if index < 4:
                self.assertEqual(actual.index(max(actual)), color.index(max(color)))
        self.assertGreaterEqual(result.getpixel((40, 60))[0], 230)
        self.assertGreater(result.getpixel((100, 60))[0], result.getpixel((40, 60))[0])

    def test_without_detected_card_reference_profile_is_unchanged(self):
        pixel, _ = self.prepare((176, 217, 233), (150, 190, 210), rectify=False)
        for actual, expected in zip(pixel, (176, 217, 233)):
            self.assertAlmostEqual(actual, expected, delta=2)

    def test_ocr_and_llm_match_pre_change_fixture_hashes(self):
        # Generated PNG scene, not personal card data. Digests were recorded
        # from prepare_card_images before adding profile-only white balance.
        expected = {
            0: ("c95f7c3805db861063d3b7fcffe3d21d3b9a9808bb016cafce759f2ecce23407", "23c1edcd323d72c98f16071598b08b92b2553b6cfb655b7b477214c33aacd1a8"),
            1: ("c632bee6c898dddbfbfb54668a9ebc08b3a337fd7b155101cf281e165476be18", "32dfbaccea8950064d30372995e69872f427b3617ebd9324875266ffc3b902f2"),
        }
        for quarters, hashes in expected.items():
            with self.subTest(quarters=quarters):
                _, prepared = self.prepare((176, 217, 233), (150, 190, 210), quarters)
                actual = tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in (prepared.ocr_path, prepared.llm_path))
                self.assertEqual(actual, hashes)

    def assert_whitening_failure_preserves_images(self, target):
        with patch.object(image_processing, "_white_balance_profile", side_effect=lambda image: image):
            _, baseline = self.prepare((176, 217, 233), (150, 190, 210))
        expected = tuple(path.read_bytes() for path in (
            baseline.profile_path, baseline.ocr_path, baseline.llm_path,
        ))
        whiten = image_processing._white_balance_profile
        operations = []

        def fail_whitening(image):
            # OCR autocontrast also uses Image.point; fail only profile whitening.
            with patch(target, side_effect=RuntimeError("synthetic private error")) as operation:
                operations.append(operation)
                return whiten(image)

        with patch.object(image_processing, "_white_balance_profile", side_effect=fail_whitening):
            with self.assertLogs("bizcard", level="WARNING") as logs:
                _, prepared = self.prepare((176, 217, 233), (150, 190, 210))
        self.assertEqual(len(operations), 1)
        operations[0].assert_called_once()
        self.assertEqual(tuple(path.read_bytes() for path in (
            prepared.profile_path, prepared.ocr_path, prepared.llm_path,
        )), expected)
        self.assertEqual(logs.output, ["WARNING:bizcard:profile paper whitening skipped"])
        self.assertIsNone(logs.records[0].exc_info)

    def test_numpy_failure_preserves_profile_ocr_and_llm(self):
        self.assert_whitening_failure_preserves_images("numpy.asarray")

    def test_pillow_lut_failure_preserves_profile_ocr_and_llm(self):
        self.assert_whitening_failure_preserves_images("PIL.Image.Image.point")

    def test_cli_import_and_python_check_without_numpy(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, "-c", "import sys; sys.modules['numpy'] = None; "
                 "from bizcard_intake import cli; "
                 "check = cli.doctor.check_python(); "
                 "assert check.ok, check.detail; print(check.detail)"],
                cwd=folder, env={"PYTHONPATH": str(root), "HOME": folder},
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("numpy", result.stdout)
        self.assertIn("종이색 보정", result.stdout)
        self.assertIn("경고", result.stdout)


if __name__ == "__main__":
    unittest.main()

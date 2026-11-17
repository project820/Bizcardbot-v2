import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops

from bizcard_intake import image_processing
from bizcard_intake.image_processing import CANVAS_SIZE


class ImageProcessingTest(unittest.TestCase):
    def test_profile_image_is_square_white_canvas_and_preserves_card_ratio(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "card.jpg"
            output = tmp_path / "profile" / "card_profile.jpg"

            card = Image.new("RGB", (800, 500), (235, 235, 230))
            card.save(source, format="JPEG")

            with patch.object(image_processing, "_try_vision_rectify", lambda source_path, rectified_path: None), patch.object(
                image_processing, "_try_opencv_rectify", lambda source_path, output_path: None
            ):
                prepared = image_processing.prepare_card_images(source, output)
            profile = Image.open(prepared.profile_path).convert("RGB")

            self.assertEqual(profile.size, (CANVAS_SIZE, CANVAS_SIZE))
            self.assertEqual(profile.getpixel((0, 0)), (255, 255, 255))
            self.assertTrue(prepared.ocr_path.exists())
            with Image.open(prepared.llm_path) as llm:
                self.assertLessEqual(max(llm.size), image_processing.LLM_MAX_SIDE)

            diff = ImageChops.difference(profile, Image.new("RGB", profile.size, "white"))
            bbox = diff.getbbox()
            self.assertIsNotNone(bbox)
            left, top, right, bottom = bbox
            ratio = (right - left) / (bottom - top)
            self.assertAlmostEqual(ratio, 800 / 500, delta=0.08)


class OrientationImageTest(unittest.TestCase):
    def test_portrait_fallback_stays_portrait(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "card.jpg"
            Image.new("RGB", (300, 500), "navy").save(source)
            with patch.object(image_processing, "_try_vision_rectify", return_value=None), patch.object(image_processing, "_try_opencv_rectify", return_value=None):
                prepared = image_processing.prepare_card_images(source, Path(tmp) / "profile" / "card.jpg")
            with Image.open(prepared.ocr_path) as image:
                self.assertLess(image.width, image.height)

    def test_rotation_reuses_rectification_and_turns_all_three_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "card.jpg"
            card = Image.new("RGB", (500, 300), "navy")
            card.paste("red", (0, 0, 250, 150))
            card.save(source)
            with patch.object(image_processing, "_try_vision_rectify", return_value=source) as rectify, patch.object(image_processing, "_try_opencv_rectify", side_effect=AssertionError("no second rectification")):
                first = image_processing.prepare_card_images(source, Path(tmp) / "profile" / "card.jpg")
                self.assertIn("rotate_quarters", __import__("inspect").signature(image_processing.prepare_card_images).parameters)
                prepared = image_processing.prepare_card_images(source, first.profile_path, rotate_quarters=1, reuse=first)
                self.assertEqual(rectify.call_count, 1)
            for path in (prepared.ocr_path, prepared.llm_path):
                with Image.open(path) as image:
                    self.assertLess(image.width, image.height)
                    self.assertGreater(image.getpixel((image.width * 3 // 4, image.height // 4))[0], 200)
            with Image.open(prepared.profile_path) as profile:
                bbox = ImageChops.difference(profile, Image.new("RGB", profile.size, "white")).getbbox()
                self.assertLess(bbox[2] - bbox[0], bbox[3] - bbox[1])

    def test_clockwise_quarters_rotate_pixels_without_changing_rectifier_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "card.jpg"
            image = Image.new("RGB", (500, 300), "navy")
            image.paste("red", (0, 0, 250, 150))
            image.save(source)
            original = source.read_bytes()
            reuse = image_processing.PreparedImages(source, source, source, source, "vision")
            for quarters, corner in ((1, (0.65, 0.35)), (2, (0.65, 0.65)), (3, (0.35, 0.65))):
                with self.subTest(quarters=quarters), patch.object(image_processing, "_try_vision_rectify", side_effect=AssertionError("reuse must not rectify")):
                    prepared = image_processing.prepare_card_images(source, Path(tmp) / "profile" / "card.jpg", rotate_quarters=quarters, reuse=reuse)
                    for path in (prepared.profile_path, prepared.ocr_path, prepared.llm_path):
                        with Image.open(path) as output:
                            left, top, right, bottom = ImageChops.difference(output, Image.new("RGB", output.size, "white")).getbbox()
                            red = output.getpixel((int(left + (right - left) * corner[0]), int(top + (bottom - top) * corner[1])))
                            self.assertGreater(red[0], 200)
                            self.assertLess(red[2], 60)
            self.assertEqual(source.read_bytes(), original)

    def test_exif_normalization_precedes_explicit_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "card.jpg"
            image = Image.new("RGB", (500, 300), "navy")
            image.paste("red", (0, 0, 250, 150))
            exif = Image.Exif()
            exif[274] = 6  # EXIF says display 90 degrees clockwise.
            image.save(source, exif=exif)
            reuse = image_processing.PreparedImages(source, source, source, None, "original")
            prepared = image_processing.prepare_card_images(source, Path(tmp) / "profile" / "card.jpg", rotate_quarters=1, reuse=reuse)
            with Image.open(prepared.ocr_path) as output:
                self.assertGreater(output.width, output.height)
                self.assertGreater(output.getpixel((output.width * 3 // 4, output.height * 3 // 4))[0], 200)



class HelperBuildTest(unittest.TestCase):
    def test_helpers_compile_once_atomically_for_concurrent_workers(self):
        from bizcard_intake import vision_ocr
        for module, prefix, ensure in ((vision_ocr, 'VISION_OCR', vision_ocr._ensure_vision_ocr),
                                       (image_processing, 'VISION_RECTIFY', image_processing._ensure_vision_rectifier)):
            with self.subTest(helper=prefix), tempfile.TemporaryDirectory() as tmp:
                source, binary = Path(tmp) / 'helper.swift', Path(tmp) / 'helper'
                source.write_text('fake swift')
                calls = []
                def compile(args, **kwargs):
                    output = Path(args[-1])
                    calls.append(output)
                    self.assertNotEqual(output, binary)
                    self.assertFalse(binary.exists())
                    time.sleep(0.03)
                    output.write_bytes(b'compiled')
                    return SimpleNamespace(returncode=0, stderr='')
                with patch.object(module, prefix + '_SOURCE', source), patch.object(module, prefix + '_BINARY', binary), patch.object(module.subprocess, 'run', side_effect=compile):
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        results = list(pool.map(lambda _: ensure(), range(8)))
                self.assertEqual(results, [binary] * 8)
                self.assertEqual(len(calls), 1)
                self.assertEqual(binary.read_bytes(), b'compiled')
                self.assertFalse(calls[0].exists())


if __name__ == "__main__":
    unittest.main()

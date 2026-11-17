"""On macOS, verify the real Vision helper against synthetic system-font text."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bizcard_intake.vision_ocr import extract_text, vote_orientation
from git_baseline import require_tag


@unittest.skipUnless(sys.platform == 'darwin', 'Apple Vision requires macOS')
class CjkVisionTest(unittest.TestCase):
    def test_horizontal_japanese_and_chinese_are_recognized_in_original_script(self):
        from PIL import Image, ImageDraw, ImageFont

        font_root = Path('/System/Library/Fonts')
        japanese = next(font_root.glob('*W3.ttc'), None)
        pingfang = next(Path('/System/Library/AssetsV2').glob('com_apple_MobileAsset_Font*/**/PingFang.ttc'), None)
        if japanese is None or pingfang is None:
            self.skipTest('Hiragino Sans and PingFang system fonts are required')
        cases = [
            (japanese, 0, '株式会社テスト', '山田 太郎', '部長'),
            (pingfang, 3, '测试有限公司', '王小明', '经理'),
            (pingfang, 2, '測試股份有限公司', '陳美玲', '經理'),
        ]
        with tempfile.TemporaryDirectory(prefix='bizcard-cjk-test-') as folder:
            for index, (path, face, company, name, title) in enumerate(cases):
                with self.subTest(company=company):
                    font = ImageFont.truetype(str(path), 60, index=face)
                    image = Image.new('RGB', (1400, 900), 'white')
                    draw = ImageDraw.Draw(image)
                    for y, text in zip((100, 250, 400, 600), (company, name, title, '+81 90-0000-0000')):
                        draw.text((80, y), text, font=font, fill='black')
                    target = Path(folder) / f'card-{index}.png'
                    image.save(target)
                    result = extract_text(target)
                    text = ''.join(result.text.split())
                    for expected in (company, name, title):
                        self.assertIn(''.join(expected.split()), text)
                    self.assertEqual(vote_orientation(result.lines, *image.size)[0], 0)

    def test_korean_request_preserves_v211_lines_and_geometry(self):
        from PIL import Image, ImageDraw, ImageFont
        from bizcard_intake.vision_ocr import _ensure_vision_ocr

        font_path = Path('/System/Library/Fonts/AppleSDGothicNeo.ttc')
        if not font_path.exists():
            self.skipTest('Korean system font is required')
        baseline_source = """
import Foundation
import Vision
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["ko-KR", "en-US"]
try VNImageRequestHandler(url: URL(fileURLWithPath: CommandLine.arguments[1]), options: [:]).perform([request])
let rows: [[String: Any]] = (request.results ?? []).compactMap { observation in
    guard let candidate = observation.topCandidates(1).first else { return nil }
    return ["text": candidate.string, "confidence": candidate.confidence,
            "topLeft": ["x": observation.topLeft.x, "y": observation.topLeft.y],
            "topRight": ["x": observation.topRight.x, "y": observation.topRight.y]]
}
FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: rows))
"""
        with tempfile.TemporaryDirectory(prefix='bizcard-korean-preserve-') as folder:
            root = Path(folder)
            source, binary, target = root / 'baseline.swift', root / 'baseline', root / 'card.png'
            source.write_text(baseline_source)
            subprocess.run(['swiftc', str(source), '-O', '-o', str(binary)], check=True, capture_output=True)
            image = Image.new('RGB', (1600, 1000), 'white')
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype(str(font_path), 50)
            for y, text in zip((60, 200, 340, 480, 620),
                               ('한국어 보존 검증을 위한 가상 회사', '홍길동 영업팀장 컨설턴트',
                                '서울특별시 중구 가상로 합성 명함', '테스트 주식회사 연구개발부',
                                '010-5555-0100 synthetic@example.com')):
                draw.text((80, y), text, font=font, fill='black')
            image.save(target)
            baseline = json.loads(subprocess.check_output([str(binary), str(target)]))
            actual = json.loads(subprocess.check_output([str(_ensure_vision_ocr()), str(target)]))
            self.assertGreaterEqual(sum('가' <= c <= '힣' for row in baseline for c in row['text']), 30)
            self.assertEqual(actual, baseline)

    def test_k9_hangul_name_survives_adopted_cjk_pass_and_prompt_is_v211(self):
        require_tag(self)
        from PIL import Image, ImageDraw, ImageFont
        from bizcard_intake import parser
        from types import ModuleType
        baseline = ModuleType('bizcard_intake._baseline_parser')
        baseline.__package__ = 'bizcard_intake'
        baseline.__file__ = parser.__file__
        exec(subprocess.check_output(['git', 'show', 'v2.1.1:bizcard_intake/parser.py']).decode(), baseline.__dict__)
        with tempfile.TemporaryDirectory() as folder:
            image = Image.new('RGB', (1050, 600), 'white')
            draw = ImageDraw.Draw(image)
            y = 60
            for text, size in [('김민준', 60), ('代表理事', 36), ('大韓製粉株式會社', 36),
                               ('010-0000-6789', 30), ('mj@example.com', 30)]:
                font = '/System/Library/Fonts/' + ('Helvetica.ttc' if text.isascii() else 'AppleSDGothicNeo.ttc')
                draw.text((70, y), text, fill='black', font=ImageFont.truetype(font, size))
                y += int(size * 1.6)
            target = Path(folder)/'k9.png'; image.save(target)
            result = extract_text(target)
            self.assertIn('김민준', result.text)
            self.assertIn('代表理事', result.text)
            self.assertLess(result.text.index('김민준'), result.text.index('代表理事'))
            for side in ('front', 'back'):
                self.assertEqual(parser.card_prompt(result, side).encode(), baseline.card_prompt(result, side).encode())

    def test_identical_overlapping_hangul_lines_are_not_duplicated(self):
        from PIL import Image, ImageDraw, ImageFont
        with tempfile.TemporaryDirectory() as folder:
            image = Image.new('RGB', (1050, 600), 'white')
            draw = ImageDraw.Draw(image)
            y = 60
            for text, size in [('대표이사  박지훈', 50), ('韓國産業株式會社', 40),
                               ('010-0000-8901', 30), ('jh@example.com', 30)]:
                font = '/System/Library/Fonts/' + ('Helvetica.ttc' if text.isascii() else 'AppleSDGothicNeo.ttc')
                draw.text((70, y), text, fill='black', font=ImageFont.truetype(font, size))
                y += int(size * 1.6)
            target = Path(folder)/'mixed.png'
            image.save(target)
            result = extract_text(target)
            names = [line.text for line in result.lines if '박지훈' in line.text]
            self.assertEqual(len(names), 1, names)

    def test_macOS11_deployment_target_typechecks(self):
        import platform
        result = subprocess.run(['swiftc', '-typecheck', '-target', platform.machine()+'-apple-macos11',
                                 'scripts/vision_ocr.swift'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_optional_17_fixtures_preserve_v211_json_and_orientation(self):
        from PIL import Image
        from bizcard_intake.vision_ocr import _ensure_vision_ocr, OcrLine, _point
        root = Path('/tmp/bizcard-cjk/fixtures')
        require_tag(self)
        paths = sorted(p for p in root.rglob('*') if p.suffix.lower() in ('.png', '.jpg', '.jpeg'))
        if not root.exists():
            self.skipTest('local 17-fixture corpus absent')
        self.assertGreaterEqual(len(paths), 17)
        with tempfile.TemporaryDirectory() as folder:
            source, binary = Path(folder)/'baseline.swift', Path(folder)/'baseline'
            source.write_bytes(subprocess.check_output(['git', 'show', 'v2.1.1:scripts/vision_ocr.swift']))
            subprocess.run(['swiftc', str(source), '-O', '-o', str(binary)], capture_output=True, check=True)
            for index, path in enumerate(paths):
                before = json.loads(subprocess.check_output([str(binary), str(path)]))
                after = json.loads(subprocess.check_output([str(_ensure_vision_ocr()), str(path)]))
                # Never include private OCR text in assertion output.
                self.assertTrue(before == after, f'fixture {index + 1} OCR differs')
                def lines(rows):
                    return [OcrLine(r['text'], r['confidence'], _point(r['topLeft']), _point(r['topRight'])) for r in rows]
                with Image.open(path) as img:
                    self.assertTrue(vote_orientation(lines(before), *img.size) == vote_orientation(lines(after), *img.size),
                                    f'fixture {index + 1} orientation differs')

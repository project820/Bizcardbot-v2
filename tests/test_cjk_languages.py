"""Synthetic CJK regressions; no live contacts or API calls."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import contacts, parser
from bizcard_intake.draft import CardDraft
from bizcard_intake.vision_ocr import OcrLine, OcrResult, vote_orientation


class CjkLanguageTest(unittest.TestCase):
    def test_vision_enables_all_card_languages_with_correction(self):
        source = (Path(__file__).resolve().parents[1] / 'scripts/vision_ocr.swift').read_text()
        for language in ('ko-KR', 'en-US', 'ja-JP', 'zh-Hans', 'zh-Hant'):
            self.assertIn('"' + language + '"', source)
        self.assertIn('request.recognitionLevel = .accurate', source)
        self.assertIn('request.usesLanguageCorrection = true', source)

    def test_vision_detects_script_instead_of_always_using_first_korean_language(self):
        source = (Path(__file__).resolve().parents[1] / 'scripts/vision_ocr.swift').read_text()
        self.assertIn('cjkRequest.automaticallyDetectsLanguage = true', source)
        self.assertNotIn('request.automaticallyDetectsLanguage = true', source)

    def test_prompt_preserves_original_names_readings_companies_and_addresses(self):
        prompt = parser.card_prompt(OcrResult((OcrLine('株式会社テスト', 1),)), 'front')
        for instruction in ('성 먼저', '원문', '후리가나', '로마자', '병음', 'notes',
                            '株式会社', '有限公司', '股份有限公司', '+81', '+86', '+852', '+886',
                            '현지 번호', '주소', '部長', '经理'):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, prompt)
        schema = json.loads(parser.SCHEMA_PATH.read_text())
        self.assertFalse(any('phonetic' in key for key in schema['properties']))

    def test_cjk_display_names_keep_original_script_and_short_title(self):
        cases = [
            ({'name': '山田 太郎', 'title': '部長/Sales Director', 'company': '株式会社テスト'},
             '山田 太郎 部長 株式会社テスト'),
            ({'name': '王小明', 'title': '经理（Manager）', 'company': '测试有限公司'},
             '王小明 经理 测试有限公司'),
            ({'name': '陳美玲', 'title': '經理／Manager', 'company': '測試股份有限公司'},
             '陳美玲 經理 測試股份有限公司'),
            ({'name': '李小華', 'title': '営業部 部長', 'display_title': '部長', 'company': '有限会社テスト'},
             '李小華 部長 有限会社テスト'),
        ]
        for data, expected in cases:
            with self.subTest(name=data['name']):
                self.assertEqual(CardDraft.from_dict(data).display_name, expected)

    def test_korean_display_names_stay_byte_identical(self):
        cases = [
            ({'name': '최민준', 'title': '차장', 'company': '(주) 다솔이엔지'}, '최민준 차장 (주)다솔이엔지'),
            ({'name': '정호준', 'title': '영업팀장/컨설턴트 (Sales Executive)', 'company': '누리네트웍스'},
             '정호준 영업팀장 누리네트웍스'),
            ({'name': '윤대현', 'title': '과장', 'display_title': '과장', 'company': '한빛에어테크'},
             '윤대현 과장 한빛에어테크'),
        ]
        for data, expected in cases:
            self.assertEqual(CardDraft.from_dict(data).display_name.encode(), expected.encode())

    def test_orientation_votes_cjk_in_all_four_directions(self):
        for text in ('山田太郎株式会社テスト', '王小明测试有限公司', '陳美玲測試股份有限公司'):
            for quarters, right in enumerate(((0.9, 0.5), (0.1, 0.9), (-0.7, 0.5), (0.1, 0.1))):
                self.assertEqual(vote_orientation([OcrLine(text, 1, (0.1, 0.5), right)], 1600, 1000),
                                 (quarters, 1.0, float(len(text))))

    def test_portrait_card_is_not_rotated_for_its_aspect_ratio(self):
        lines = [OcrLine('山田太郎', 1, (0.1, 0.8), (0.8, 0.8)), OcrLine('部長', 1, (0.1, 0.5), (0.8, 0.5))]
        self.assertEqual(vote_orientation(lines, 900, 1500)[0], 0)
        # One-character vertical glyph observations must not invent rotation votes.
        glyphs = [OcrLine(char, 1, (0.7, y), (0.8, y)) for char, y in zip('山田太郎', (.8, .7, .6, .5))]
        self.assertEqual(vote_orientation(glyphs, 900, 1500), (0, 0.0, 0.0))

    def test_parser_uses_existing_single_request_for_cjk(self):
        fields = {'name': '山田太郎', 'title': '部長', 'company': '株式会社テスト', 'mobile': ['+81 90-0000-0000']}
        with patch.object(parser, 'crosscheck_enabled', return_value=False), \
             patch.object(parser, 'run_backend', return_value=fields) as backend:
            result = parser.parse_card(Path('/synthetic/ja.png'), OcrResult(()), 'front')
        self.assertEqual(result.name, '山田太郎')
        backend.assert_called_once()


class CjkPhoneTest(unittest.TestCase):
    def test_international_dedupe_preserves_country_and_removes_optional_trunk(self):
        cases = [
            ('+81 90-0000-0000', '819000000000'),
            ('0081 90-0000-0000', '819000000000'),
            ('+81 (0)90-0000-0000', '819000000000'),
            ('+81 090-0000-0000', '819000000000'),
            ('+86 138-0000-0000', '8613800000000'),
            ('+86 010-5555-0100', '861055550100'),
            ('0086 10-5555-0100', '861055550100'),
            ('+852 5555-0100', '85255550100'),
            ('+886 02-5555-0100', '886255550100'),
            ('＋８１ （０）９０－００００－００００', '819000000000'),
            ('＋８６ １３８－００００－００００', '8613800000000'),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(contacts.normalize_phone(text), expected)
        self.assertNotEqual(contacts.normalize_phone('+81 90-0000-0000'), contacts.normalize_phone('+86 90-0000-0000'))

    def test_international_prefix_never_becomes_korean_country_code(self):
        for text, expected in [('0081 90-0000-0000', '+81 90-0000-0000'),
                               ('0086 10-5555-0100', '+86 10-5555-0100'),
                               ('00852 5555-0100', '+852 5555-0100'),
                               ('00886 2-5555-0100', '+886 2-5555-0100')]:
            with self.subTest(text=text):
                self.assertEqual(contacts.international_phone(text), expected)
        for text in ('+81 90-0000-0000', '+86 138-0000-0000', '+852 5555-0100', '+886 2-5555-0100'):
            self.assertEqual(contacts.international_phone(text), text)
        self.assertEqual(contacts.international_phone('００８１ ９０－００００－００００'), '+81 90-0000-0000')

    def test_korean_phone_normalization_is_unchanged(self):
        for text in ('+82 (0)10-5555-0100', '0082 10 5555 0100', '82-10-5555-0100', '010-5555-0100'):
            self.assertEqual(contacts.normalize_phone(text), '01055550100')
        self.assertEqual(contacts.international_phone('010-5555-0100'), '+82 10-5555-0100')
        self.assertEqual(contacts.international_phone('02-555-0100'), '+82 2-555-0100')

    def test_unambiguous_foreign_local_formats_are_not_given_a_korean_prefix(self):
        for text in ('03-5555-0100', '(03) 5555-0100', '06-5555-0100',
                     '090-0000-0000', '09000000000', '138-0000-0000', '5555-0100'):
            with self.subTest(text=text):
                self.assertEqual(contacts.international_phone(text), text)

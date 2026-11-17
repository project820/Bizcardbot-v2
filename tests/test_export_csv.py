import csv
import io
import os
import unittest
from unittest.mock import patch

from bizcard_intake import cards, export_csv


class ExportCsvTest(unittest.TestCase):
    def decode(self, content):
        self.assertTrue(content.startswith(b'\xef\xbb\xbf'))
        text = content.decode('utf-8-sig')
        self.assertTrue(text.endswith('\r\n'))
        self.assertTrue(all(line.startswith('"') for line in text.split('\r\n') if line))
        return list(csv.reader(io.StringIO(text, newline='')))

    def test_saved_chat_drafts_and_all_columns(self):
        card = {'created_at': '2026-11-17T18:00:00+0000', 'caption': '전시회',
                'draft': {'name': 'Example', 'title': '팀장', 'company': 'Company',
                          'mobile': ['010-0000-0001', '010-5555-0001'], 'work': [], 'fax': [],
                          'email': ['user@example.com'], 'website': ['https://example.com'],
                          'address': ['테스트로 1'], 'notes': '메모'},
                'save_action': 'created', 'cost': {'krw': 1.25}}
        with patch.object(cards, 'list_cards', return_value=[card]) as listed, \
             patch.dict(os.environ, {'BIZCARD_TZ': 'Asia/Seoul'}):
            content, count = export_csv.build(123)
        listed.assert_called_once_with(123, ['saved'])
        rows = self.decode(content)
        self.assertEqual(count, 1)
        self.assertEqual(rows[0], ['받은 날짜', '이름', '직함', '회사', '휴대전화', '회사전화', '팩스',
                                   '이메일', '웹사이트', '주소', '메모', '만남 메모', '신규/갱신', 'AI 비용(원)'])
        self.assertEqual(rows[1], ['2026-11-18', 'Example', '팀장', 'Company',
                                   '010-0000-0001 / 010-5555-0001', '', '', 'user@example.com',
                                   'https://example.com', '테스트로 1', '메모', '전시회', '신규', '1.25'])

    def test_formula_cells_are_escaped_including_leading_whitespace(self):
        for value in ('=1+1', '+cmd', '-1', '@SUM(A1)', '  =1', '\t@cmd', '\r=1'):
            card = {'draft': {'name': value, 'notes': 'a,"quoted"\nline'}, 'save_action': 'updated'}
            with patch.object(cards, 'list_cards', return_value=[card]):
                content, count = export_csv.build('123')
            rows = list(csv.reader(io.StringIO(content.decode('utf-8-sig'), newline='')))
            with self.subTest(value=value):
                self.assertEqual(rows[1][1], "'" + value)
                self.assertEqual(rows[1][10], 'a,"quoted"\nline')
                self.assertEqual(rows[1][12], '갱신')
                self.assertEqual(rows[1][13], '')

    def test_cost_is_rounded_to_two_decimal_places(self):
        for value, expected in [(2.0400000003, '2.04'), (1, '1.00')]:
            with patch.object(cards, 'list_cards', return_value=[{'cost': {'krw': value}}]):
                content, _ = export_csv.build('42')
            self.assertEqual(self.decode(content)[1][-1], expected)

    def test_empty_export_has_header_and_no_cards(self):
        with patch.object(cards, 'list_cards', return_value=[]):
            content, count = export_csv.build('123')
        self.assertEqual(count, 0)
        self.assertEqual(len(self.decode(content)), 1)

    def test_legacy_card_missing_fields_and_unknown_action_stay_empty(self):
        with patch.object(cards, 'list_cards', return_value=[{}]):
            content, count = export_csv.build('123')
        self.assertEqual(self.decode(content)[1], [''] * 14)
        self.assertEqual(count, 1)


if __name__ == '__main__':
    unittest.main()
